using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Threading.RateLimiting;
using Microsoft.AspNetCore.HttpOverrides;
using Microsoft.AspNetCore.Http.Timeouts;
using Microsoft.AspNetCore.RateLimiting;
using Yarp.ReverseProxy.Forwarder;
using Yarp.ReverseProxy.Model;
using Yarp.ReverseProxy.Transforms;

var builder = WebApplication.CreateBuilder(args);
var configuration = builder.Configuration;
builder.WebHost.ConfigureKestrel(options =>
{
    options.AddServerHeader = false;
    configuration.GetSection("Kestrel:Limits").Bind(options.Limits);
});
var proxies = configuration.GetSection("ForwardedHeaders:KnownProxies").Get<string[]>() ?? [];
builder.Services.Configure<ForwardedHeadersOptions>(options =>
{
    options.KnownProxies.Clear();
    options.KnownIPNetworks.Clear();
    foreach (var proxy in proxies) options.KnownProxies.Add(IPAddress.Parse(proxy));
    options.ForwardedHeaders = proxies.Length == 0 ? ForwardedHeaders.None
        : ForwardedHeaders.XForwardedFor | ForwardedHeaders.XForwardedProto | ForwardedHeaders.XForwardedHost;
    options.ForwardLimit = configuration.GetValue<int>("ForwardedHeaders:ForwardLimit", 1);
    foreach (var host in (configuration["AllowedHosts"] ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries))
        options.AllowedHosts.Add(host);
});
builder.Services.AddReverseProxy()
    .LoadFromConfig(configuration.GetSection("ReverseProxy"))
    .AddTransforms(context =>
    {
        foreach (var header in new[] { "Forwarded", "X-Original-For", "X-Original-Proto", "X-Original-Host",
            "X-Original-Prefix", "X-Actor", "X-User", "X-User-Id", "X-Organization-Id", "X-Authenticated-User",
            "X-Client-Cert", "X-Forwarded-Client-Cert", "X-Http-Method-Override", "X-Http-Method", "X-Method-Override" })
            context.AddRequestHeaderRemove(header);
        context.AddResponseTransform(response =>
        {
            if (response.ProxyResponse is null && response.HttpContext.Features.Get<IHttpRequestTimeoutFeature>()
                ?.RequestTimeoutToken.IsCancellationRequested == true)
                response.HttpContext.Response.StatusCode = StatusCodes.Status504GatewayTimeout;
            return ValueTask.CompletedTask;
        });
        context.AddResponseHeaderRemove("Server");
        context.AddResponseHeaderRemove("X-Powered-By");
    });
builder.Services.AddCors(options => options.AddDefaultPolicy(policy => policy
    .WithOrigins(configuration.GetSection("Cors:AllowedOrigins").Get<string[]>() ?? [])
    .AllowAnyHeader().AllowAnyMethod()));
builder.Services.AddHealthChecks();
builder.Services.AddRequestTimeouts();
var batchCost = configuration.GetValue<int>("Traffic:BatchMaxOperations", 50);
var batchBudget = configuration.GetValue<int>("Traffic:BatchOperationBudget", 1000);
if (batchCost < 1 || batchBudget < batchCost) throw new InvalidOperationException("Presupuesto de operaciones batch inválido.");
builder.Services.AddRateLimiter(options =>
{
    options.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    options.AddPolicy("api", context => RateLimitPartition.GetFixedWindowLimiter(
        context.Connection.RemoteIpAddress?.ToString() ?? "unknown", _ => new FixedWindowRateLimiterOptions
        {
            PermitLimit = configuration.GetValue<int>("Traffic:RequestsPerWindow", 1000),
            Window = TimeSpan.FromSeconds(configuration.GetValue<int>("Traffic:WindowSeconds", 60)),
            QueueLimit = 0,
            AutoReplenishment = true
        }));
    options.AddPolicy("batch", context => RateLimitPartition.GetFixedWindowLimiter(
        context.Connection.RemoteIpAddress?.ToString() ?? "unknown", _ => new FixedWindowRateLimiterOptions
        {
            PermitLimit = batchBudget / batchCost,
            Window = TimeSpan.FromSeconds(configuration.GetValue<int>("Traffic:WindowSeconds", 60)),
            QueueLimit = 0, AutoReplenishment = true
        }));
    options.GlobalLimiter = PartitionedRateLimiter.Create<HttpContext, string>(context =>
        RateLimitPartition.GetConcurrencyLimiter(
            context.Request.Path.StartsWithSegments("/api/desastres/batch") || context.Request.Path.StartsWithSegments("/api/desastres/sync")
                ? "synchronization" : "gateway", key => new ConcurrencyLimiterOptions
        {
            PermitLimit = key == "synchronization" ? configuration.GetValue<int>("Traffic:SyncConcurrentRequests", 2)
                : configuration.GetValue<int>("Traffic:ConcurrentRequests", 100),
            QueueLimit = 0
        }));
    options.OnRejected = async (context, _) =>
    {
        var seconds = context.Lease.TryGetMetadata(MetadataName.RetryAfter, out var retryAfter)
            ? Math.Max(1, (int)Math.Ceiling(retryAfter.TotalSeconds)) : 1;
        context.HttpContext.Response.Headers.RetryAfter = seconds.ToString(CultureInfo.InvariantCulture);
        await Results.Problem(statusCode: 429, title: "Too many requests").ExecuteAsync(context.HttpContext);
    };
});

var app = builder.Build();
app.UseForwardedHeaders();
app.Use(async (context, next) =>
{
    var started = Stopwatch.GetTimestamp();
    var traceId = Activity.Current?.TraceId.ToString() ?? context.TraceIdentifier;
    context.Response.Headers["X-Trace-Id"] = traceId;
    try { await next(context); }
    finally
    {
        var proxy = context.Features.Get<IReverseProxyFeature>();
        var error = context.Features.Get<IForwarderErrorFeature>();
        app.Logger.LogInformation("Gateway request {TraceId} {RouteId} {Method} returned {StatusCode} via {DestinationId} in {ElapsedMs} ms",
            traceId, proxy?.Route.Config.RouteId ?? "local", context.Request.Method, context.Response.StatusCode,
            proxy?.ProxiedDestination?.DestinationId, Stopwatch.GetElapsedTime(started).TotalMilliseconds);
        if (error is not null)
            app.Logger.LogWarning("Gateway forwarding error {ForwarderError} for {RouteId} via {DestinationId}, trace {TraceId}",
                error.Error, proxy?.Route.Config.RouteId, proxy?.ProxiedDestination?.DestinationId, traceId);
    }
});
app.UseExceptionHandler(handler => handler.Run(context =>
    Results.Problem(statusCode: 500, title: "Gateway error").ExecuteAsync(context)));
app.UseRouting();
app.UseCors();
app.UseRequestTimeouts();
app.UseRateLimiter();
app.UseDefaultFiles();
app.UseStaticFiles();
app.MapHealthChecks("/health").DisableRateLimiting();
app.MapReverseProxy();
app.Run();
