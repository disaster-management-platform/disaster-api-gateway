/* global L */
const form = document.querySelector('#disaster-form');
const submitButton = document.querySelector('#submit');
const refreshButton = document.querySelector('#refresh');
const formStatus = document.querySelector('#form-status');
const listStatus = document.querySelector('#list-status');
const latitude = document.querySelector('#latitud');
const longitude = document.querySelector('#longitud');
const radius = document.querySelector('#radio');
let pendingSubmission;
const submissionStorage = 'disaster-pending-submission';
const retryButton = document.createElement('button');
retryButton.type = 'button';
retryButton.textContent = 'Reintentar el envío pendiente';
retryButton.hidden = true;
formStatus.after(retryButton);
function restoreSubmission() {
  const disaster = JSON.parse(pendingSubmission.content);
  for (const field of ['nombre', 'ubicacion', 'severidad', 'descripcion', 'latitud', 'longitud', 'radio', 'disasterTypeId', 'startedAt', 'attentionLevel'])
  {
    const input = form.elements.namedItem(field);
    if (field === 'disasterTypeId' && disaster[field] != null && !Array.from(input.options).some(option => option.value === String(disaster[field])))
      input.add(new Option(`Tipo #${disaster[field]} pendiente de consulta`, String(disaster[field])));
    if (field === 'startedAt') setDateField(input, disaster[field]);
    else input.value = disaster[field] ?? '';
  }
}
try {
  pendingSubmission = JSON.parse(sessionStorage.getItem(submissionStorage) ?? 'null');
  if (pendingSubmission) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(pendingSubmission.key))
      throw new Error('Identidad pendiente inválida.');
    restoreSubmission();
    retryButton.hidden = false;
    setStatus(formStatus, pendingSubmission.conflict ? 'Conflicto definitivo: revise y resuelva la operación antes de iniciar otra.' : 'Hay un envío sin confirmar. Reinténtalo antes de iniciar otra operación.');
    if (pendingSubmission.conflict) retryButton.textContent = 'Resolver conflicto';
  }
} catch {
  submitButton.disabled = true;
  setStatus(formStatus, 'No se pudo recuperar la operación pendiente. Revisa el almacenamiento de esta pestaña antes de continuar.', true);
}
retryButton.addEventListener('click', () => {
  if (pendingSubmission.conflict) {
    if (!window.confirm('Esta clave tiene un conflicto definitivo. ¿Descartar su reintento y preparar una nueva operación? El registro del servidor se conserva.')) return;
    sessionStorage.removeItem(submissionStorage);
    pendingSubmission = undefined;
    retryButton.hidden = true;
    retryButton.textContent = 'Reintentar el envío pendiente';
    setStatus(formStatus, 'Conflicto resuelto explícitamente. Revise los datos y pulse Registrar para iniciar una nueva operación.');
    return;
  }
  restoreSubmission();
  updateSelection();
  form.requestSubmit();
});
const colors = ['#27815b', '#738520', '#cb9223', '#d16b30', '#b83d46'];
let map;
let markers;
let selection;
let disasterTypes = [];
const transitions = { Detected: ['Registered'], Registered: ['Assessment', 'Response', 'Recovery', 'Closed'],
  Assessment: ['Response', 'Recovery', 'Closed'], Response: ['Recovery', 'Closed'],
  Recovery: ['Reconstruction', 'Closed'], Reconstruction: ['Closed'], Closed: [] };

function dateIdentity(value) {
  if (value == null) return null;
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) return NaN;
  const fraction = value.match(/\.(\d+)/)?.[1] ?? '';
  return new Date(value).toISOString().slice(0, 19) + '.' + fraction.padEnd(6, '0').slice(0, 6) + 'Z';
}
function setDateField(input, value) {
  input.value = value ? new Date(value).toISOString().slice(0, 19) : '';
  input.dataset.originalDate = value ?? '';
  input.dataset.initialValue = input.value;
  input.oninput = () => { delete input.dataset.originalDate; };
}
function creationContent(data, dateField) {
  return { nombre: data.get('nombre'), ubicacion: data.get('ubicacion'), severidad: Number(data.get('severidad')),
    descripcion: data.get('descripcion'), latitud: Number(data.get('latitud')), longitud: Number(data.get('longitud')),
    radio: data.get('radio') === '' ? null : Number(data.get('radio')),
    disasterTypeId: data.get('disasterTypeId') ? Number(data.get('disasterTypeId')) : null,
    startedAt: dateField?.dataset.originalDate && data.get('startedAt') === dateField.dataset.initialValue ? dateField.dataset.originalDate
      : data.get('startedAt') ? new Date(data.get('startedAt') + 'Z').toISOString() : null,
    attentionLevel: data.get('attentionLevel') ? Number(data.get('attentionLevel')) : null };
}
function sameCreation(left, right) {
  return ['nombre', 'ubicacion', 'descripcion'].every(field => typeof left[field] === 'string' && typeof right[field] === 'string' && left[field].trim() === right[field].trim())
    && ['severidad', 'latitud', 'longitud'].every(field => Number.isFinite(left[field]) && left[field] === right[field])
    && ['radio', 'disasterTypeId', 'attentionLevel'].every(field => (left[field] ?? null) === (right[field] ?? null))
    && (left.startedAt == null && right.startedAt == null || left.startedAt != null && right.startedAt != null
      && dateIdentity(left.startedAt) === dateIdentity(right.startedAt));
}

function setStatus(element, message, isError = false) {
  element.textContent = message;
  element.dataset.error = String(isError);
}

if (typeof L !== 'undefined') {
  map = L.map('map').setView([4.5709, -74.2973], 5);
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map);
  tiles.on('tileerror', () => {
    document.querySelector('#map-hint').textContent = 'No se pudo cargar el mapa base. Revisa tu conexión a internet. Puedes ingresar las coordenadas manualmente.';
  });
  markers = L.featureGroup().addTo(map);
  map.on('click', ({ latlng }) => {
    const point = latlng.wrap();
    latitude.value = point.lat.toFixed(6);
    longitude.value = point.lng.toFixed(6);
    updateSelection();
  });
} else {
  document.querySelector('#map-hint').textContent = 'El mapa no está disponible. Puedes escribir las coordenadas y consultar los reportes en la lista.';
}

function updateSelection() {
  if (!map) return;
  if (selection) { selection.remove(); selection = undefined; }
  if (!latitude.value || !longitude.value || !latitude.validity.valid || !longitude.validity.valid) return;
  const radio = radius.value.trim() === '' ? null : Number(radius.value);
  if (radio !== null && (!Number.isFinite(radio) || radio <= 0 || radio > 1000000)) return;
  const point = [Number(latitude.value), Number(longitude.value)];
  const style = { color: '#193c36', fillColor: '#fff', fillOpacity: .9, weight: 3, dashArray: '4 3' };
  selection = (radio === null ? L.circleMarker(point, { ...style, radius: 10 })
    : L.circle(point, { ...style, radius: radio })).addTo(map).bindTooltip('Ubicación del nuevo reporte');
}

latitude.addEventListener('input', updateSelection);
longitude.addEventListener('input', updateSelection);
radius.addEventListener('input', updateSelection);

async function api(path, options) {
  const endpoint = path.startsWith('/api/') ? path : `/api/desastres${path}`;
  const response = await (window.platformFetch ?? window.fetch)(endpoint, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    const messages = problem?.errors ? Object.values(problem.errors).flat().join(' ') : null;
    throw Object.assign(new Error(messages || `No se pudo completar la solicitud (HTTP ${response.status}). Comprueba que la API esté ejecutándose.`), { status: response.status });
  }
  const creation = options?.method === 'POST' && endpoint === '/api/desastres';
  if (creation && ![200, 201].includes(response.status)) throw new Error('El servidor no confirmó la creación. Se conserva el envío pendiente.');
  const result = response.status === 204 ? null : await response.json();
  if (creation && (!Number.isSafeInteger(result?.id) || result.id <= 0 || result.id > 2147483647 || !sameCreation(result, JSON.parse(options.body))))
    throw new Error('La respuesta no confirma el contenido original. Se conserva el envío pendiente.');
  return result;
}

function textElement(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function render(disasters, fitMap) {
  document.querySelector('#count').textContent = String(disasters.length);
  const list = document.querySelector('#disaster-list');
  list.replaceChildren();
  if (markers) markers.clearLayers();
  if (disasters.length === 0) {
    list.append(textElement('p', 'Todavía no hay reportes. Registra el primero con el formulario.', 'empty'));
    return;
  }
  for (const disaster of disasters) {
    let marker;
    if (map) {
      const popup = document.createElement('div');
      popup.append(textElement('h3', disaster.nombre, 'popup-title'),
        textElement('p', disaster.ubicacion),
        textElement('p', `Severidad: ${disaster.severidad}/5`),
        textElement('p', disaster.descripcion, 'popup-text'));
      const point = [disaster.latitud, disaster.longitud];
      const style = { color: '#fff', weight: 2, fillColor: colors[disaster.severidad - 1], fillOpacity: .9 };
      marker = (disaster.radio == null ? L.circleMarker(point, { ...style, radius: 7 + disaster.severidad })
        : L.circle(point, { ...style, radius: disaster.radio })).bindPopup(popup).addTo(markers);
    }
    const article = document.createElement('article');
    article.className = 'report';
    const content = document.createElement('div');
    content.append(textElement('h3', disaster.nombre), textElement('p', disaster.ubicacion),
      textElement('p', disaster.descripcion),
      textElement('p', `Atención: ${disaster.attentionLevel ?? 'sin clasificar'} · ${disaster.phase} · v${disaster.version}`),
      textElement('p', `Inicio UTC: ${disaster.startedAt ?? 'desconocido'} · Registro UTC: ${disaster.registeredAt ?? 'desconocido'} · Actualización UTC: ${disaster.updatedAt ?? 'desconocido'}`),
      textElement('p', `${disaster.latitud.toFixed(6)}, ${disaster.longitud.toFixed(6)}`));
    if (map) {
      const locate = textElement('button', 'Ver en el mapa ↗', 'location-button');
      locate.type = 'button';
      locate.setAttribute('aria-label', `Ver ${disaster.nombre} en el mapa`);
      locate.addEventListener('click', () => {
        map.setView(marker.getLatLng(), 12);
        marker.openPopup();
        document.querySelector('#map').scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
      content.append(locate);
    }
    if (disaster.geography?.polygon && map) L.geoJSON(disaster.geography.polygon).addTo(markers);
    const typeLabel = textElement('p', `Tipo: ${disasterTypes.find(type => type.id === disaster.disasterTypeId)?.name ?? (disaster.disasterTypeId ?? 'pendiente')}`);
    if (disaster.disasterTypeId != null) typeLabel.dataset.disasterTypeLabel = String(disaster.disasterTypeId);
    content.append(typeLabel);
    appendManagement(content, disaster);
    appendIntegration(content, disaster);
    const severity = textElement('span', `Nivel ${disaster.severidad}`, `severity level-${disaster.severidad}`);
    article.append(content, severity);
    list.append(article);
  }
  if (map && fitMap) map.fitBounds(markers.getBounds(), { padding: [35, 35], maxZoom: 12 });
}

async function loadDisasters(fitMap = true) {
  refreshButton.disabled = true;
  setStatus(listStatus, 'Cargando reportes…');
  try {
    render(await api(''), fitMap);
    setStatus(listStatus, '');
  } catch (error) {
    setStatus(listStatus, error.message, true);
  } finally {
    refreshButton.disabled = false;
  }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const radio = radius.value.trim() === '' ? null : Number(radius.value);
  if (radio !== null && (!Number.isFinite(radio) || radio <= 0 || radio > 1000000)) {
    setStatus(formStatus, 'El radio debe ser mayor que cero y máximo 1.000.000 metros.', true);
    return;
  }
  if (!form.reportValidity()) return;
  const disaster = creationContent(new FormData(form), form.elements.namedItem('startedAt'));
  if (pendingSubmission && !sameCreation(disaster, JSON.parse(pendingSubmission.content))) {
    setStatus(formStatus, 'El envío anterior sigue sin confirmar. Use su acción de reintento o resolución antes de iniciar otra operación.', true);
    return;
  }
  const content = pendingSubmission?.content ?? JSON.stringify(disaster);
  submitButton.disabled = true;
  setStatus(formStatus, 'Guardando reporte…');
  try {
    if (!pendingSubmission) {
      let key;
      if (typeof crypto.randomUUID === 'function') key = crypto.randomUUID();
      else {
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
        key = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
      }
      pendingSubmission = { content, key };
    }
    sessionStorage.setItem(submissionStorage, JSON.stringify(pendingSubmission));
    retryButton.hidden = false;
    retryButton.disabled = true;
    const saved = await api('', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pendingSubmission.key }, body: content
    });
    sessionStorage.removeItem(submissionStorage);
    pendingSubmission = undefined;
    retryButton.hidden = true;
    form.reset();
    updateSelection();
    setStatus(formStatus, `Reporte #${saved.id} registrado correctamente.`);
    await loadDisasters();
  } catch (error) {
    if (error.status === 409 && pendingSubmission) {
      pendingSubmission.conflict = true;
      sessionStorage.setItem(submissionStorage, JSON.stringify(pendingSubmission));
    }
    if (error.status === 400) {
      sessionStorage.removeItem(submissionStorage);
      pendingSubmission = undefined;
    }
    retryButton.hidden = !pendingSubmission;
    retryButton.textContent = pendingSubmission?.conflict ? 'Resolver conflicto' : 'Reintentar el envío pendiente';
    setStatus(formStatus, error.message, true);
  } finally {
    submitButton.disabled = false;
    retryButton.disabled = false;
  }
});

refreshButton.addEventListener('click', () => loadDisasters());
loadDisasters();

function refreshTypeSelect(select) {
  const selected = select.value;
  select.replaceChildren(new Option('Pendiente de determinar', ''));
  for (const type of disasterTypes) if (type.isActive || String(type.id) === selected)
    select.add(new Option(type.name + (type.isActive ? '' : ' (inactivo)'), String(type.id)));
  if (selected && !Array.from(select.options).some(option => option.value === selected))
    select.add(new Option(`Tipo #${selected} pendiente de consulta`, selected));
  select.value = selected;
}

async function loadTypes() {
  try {
    disasterTypes = await api('/api/tipos-desastre');
    for (const select of document.querySelectorAll('#disasterTypeId, select[data-type-selector]')) refreshTypeSelect(select);
    for (const label of document.querySelectorAll('[data-disaster-type-label]'))
      label.textContent = `Tipo: ${disasterTypes.find(type => String(type.id) === label.dataset.disasterTypeLabel)?.name ?? label.dataset.disasterTypeLabel}`;
    const list = document.querySelector('#type-list'); list.replaceChildren();
    for (const type of disasterTypes) {
      const row = textElement('p', type.name + (type.isActive ? '' : ' · Inactivo'));
      if (type.isActive) {
        const button = textElement('button', 'Desactivar', 'secondary'); button.type = 'button';
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            await api(`/api/tipos-desastre/${type.id}/desactivar`, { method: 'PUT' });
            await loadTypes();
          } catch (error) { setStatus(document.querySelector('#type-status'), error.message, true); button.disabled = false; }
        }); row.append(button);
      }
      list.append(row);
    }
  } catch (error) { setStatus(document.querySelector('#type-status'), error.message, true); }
}

document.querySelector('#type-form').addEventListener('submit', async event => {
  event.preventDefault();
  const input = document.querySelector('#type-name');
  try {
    await api('/api/tipos-desastre', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: input.value }) });
    input.value = '';
    setStatus(document.querySelector('#type-status'), 'Tipo agregado.');
    await loadTypes();
  } catch (error) { setStatus(document.querySelector('#type-status'), error.message, true); }
});

function appendManagement(content, disaster) {
  const edit = document.createElement('details'); edit.append(textElement('summary', 'Actualizar información'));
  const editForm = document.createElement('form');
  for (const [name, label, type, min, max] of [
    ['nombre', 'Nombre', 'text'], ['ubicacion', 'Ubicación', 'text'], ['descripcion', 'Descripción', 'text'],
    ['severidad', 'Severidad', 'number', 1, 5], ['latitud', 'Latitud', 'number', -90, 90],
    ['longitud', 'Longitud', 'number', -180, 180], ['radio', 'Radio (metros)', 'number', 0, 1000000],
    ['startedAt', 'Inicio UTC', 'datetime-local'], ['disasterTypeId', 'Tipo', 'select'], ['attentionLevel', 'Atención requerida', 'select']]) {
    const wrapper = textElement('label', label);
    let field;
    if (type === 'select') {
      field = document.createElement('select'); field.add(new Option('Desconocido', ''));
      const choices = name === 'disasterTypeId' ? disasterTypes.filter(item => item.isActive || item.id === disaster.disasterTypeId).map(item => [item.id, item.name])
        : [[1, 'Bajo'], [2, 'Medio'], [3, 'Alto'], [4, 'Crítico']];
      if (name === 'disasterTypeId' && disaster.disasterTypeId != null && !choices.some(([value]) => value === disaster.disasterTypeId))
        choices.push([disaster.disasterTypeId, `Tipo #${disaster.disasterTypeId} pendiente de consulta`]);
      for (const [value, text] of choices) field.add(new Option(text, String(value)));
    } else {
      field = document.createElement('input'); field.type = type;
      if (type === 'number') { field.step = name === 'severidad' ? '1' : 'any'; field.min = min; field.max = max; }
      if (type === 'datetime-local') field.step = '1';
    }
    field.name = name;
    if (name === 'startedAt') setDateField(field, disaster[name]);
    else field.value = disaster[name] ?? '';
    if (name === 'disasterTypeId') field.dataset.typeSelector = 'true';
    field.required = ['nombre', 'ubicacion', 'descripcion', 'severidad', 'latitud', 'longitud'].includes(name);
    if (name === 'nombre') field.maxLength = 120;
    if (name === 'ubicacion') field.maxLength = 200;
    if (name === 'descripcion') field.maxLength = 2000;
    wrapper.append(field); editForm.append(wrapper);
  }
  const save = textElement('button', 'Guardar cambios', 'secondary'); save.type = 'submit';
  const result = textElement('p', '', 'status'); result.setAttribute('role', 'status');
  editForm.append(save, result);
  editForm.addEventListener('submit', async event => {
    event.preventDefault(); save.disabled = true;
    try {
      await api(`/${disaster.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...creationContent(new FormData(editForm), editForm.elements.namedItem('startedAt')), expectedVersion: disaster.version }) });
      await loadDisasters(false);
    } catch (error) { setStatus(result, error.message, true); save.disabled = false; }
  }); edit.append(editForm); content.append(edit);
  if ((transitions[disaster.phase] ?? []).length) {
    const phaseDetails = document.createElement('details'); phaseDetails.append(textElement('summary', 'Cambiar fase'));
    const phaseForm = document.createElement('form'); const phase = document.createElement('select'); phase.name = 'phase';
    for (const value of transitions[disaster.phase]) phase.add(new Option(value, value));
    const reasonLabel = textElement('label', 'Justificación (obligatoria en saltos y cierres)');
    const reason = document.createElement('textarea'); reason.maxLength = 2000; reasonLabel.append(reason);
    const confirmationLabel = textElement('label', 'Confirmo que no quedan compromisos críticos pendientes, incluidos los de reconstrucción.');
    const confirmation = document.createElement('input'); confirmation.type = 'checkbox'; confirmationLabel.prepend(confirmation);
    const send = textElement('button', 'Cambiar fase', 'secondary'); send.type = 'submit'; const status = textElement('p', '', 'status');
    phaseForm.append(phase, reasonLabel, confirmationLabel, send, status);
    phaseForm.addEventListener('submit', async event => {
      event.preventDefault(); send.disabled = true;
      try {
        await api(`/${disaster.id}/fase`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phase: phase.value, expectedVersion: disaster.version, reason: reason.value,
            confirmNoCriticalCommitments: confirmation.checked }) });
        await loadDisasters(false);
      } catch (error) { setStatus(status, error.message, true); send.disabled = false; }
    }); phaseDetails.append(phaseForm); content.append(phaseDetails);
  }
  const history = document.createElement('details'); history.append(textElement('summary', 'Consultar historial'));
  const entries = textElement('pre', ''); history.append(entries);
  history.addEventListener('toggle', async () => {
    if (!history.open) return;
    try {
      const history = await api(`/${disaster.id}/historial`);
      entries.textContent = JSON.stringify(history.map(entry => ({ ...entry, changes: entry.changes ? JSON.parse(entry.changes) : null })), null, 2);
    }
    catch (error) { entries.textContent = error.message; }
  }); content.append(history);
}

loadTypes();

function appendIntegration(content, disaster) {
  const reports = document.createElement('details'); reports.append(textElement('summary', 'Fuentes y reportes asociados'));
  const reportList = textElement('pre', ''); reports.append(reportList);
  reports.addEventListener('toggle', async () => {
    if (!reports.open) return;
    try { reportList.textContent = JSON.stringify(await api(`/${disaster.id}/reportes`), null, 2); }
    catch (error) { reportList.textContent = error.message; }
  });
  const associate = document.createElement('form');
  const reportLabel = textElement('label', 'Identificador del reporte recibido');
  const reportId = document.createElement('input'); reportId.required = true;
  reportId.pattern = '[0-9a-fA-F-]{36}'; reportLabel.append(reportId);
  const submit = textElement('button', 'Asociar reporte', 'secondary'); submit.type = 'submit';
  const status = textElement('p', '', 'status'); associate.append(reportLabel, submit, status);
  associate.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true;
    try {
      await api(`/${disaster.id}/reportes/${encodeURIComponent(reportId.value)}`, { method: 'PUT',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expectedVersion: disaster.version }) });
      await loadDisasters(false);
    } catch (error) { setStatus(status, error.message, true); submit.disabled = false; }
  }); reports.append(associate); content.append(reports);
  for (const [path, title, fields] of [
    ['organismos', 'Organismos y responsabilidades', [
      ['registeringOrganizationId', 'Identificador del organismo registrador', disaster.registeringOrganizationId ?? '', 'input'],
      ['emergencyResponsibleId', 'Identificador del responsable de la emergencia', disaster.emergencyResponsibleId ?? '', 'input'],
      ['organizations', 'Asociaciones: JSON con organizationId y responsibility', JSON.stringify(disaster.organizations ?? [], null, 2), 'textarea']]],
    ['geografia', 'Delimitación geográfica', [
      ['sourceId', 'Identificador de fuente (si se conoce)', '', 'input'],
      ['geography', 'GeoJSON Polygon y listas municipalities, neighborhoods, riskZones', JSON.stringify(disaster.geography ?? { polygon: null, municipalities: [], neighborhoods: [], riskZones: [] }, null, 2), 'textarea']]]]) {
    const details = document.createElement('details'); details.append(textElement('summary', title));
    const editor = document.createElement('form');
    for (const [name, label, value, tag] of fields) {
      const wrapper = textElement('label', label); const field = document.createElement(tag);
      field.name = name; field.value = value; field.maxLength = tag === 'textarea' ? 65536 : 200;
      if (tag === 'textarea') field.rows = 6;
      wrapper.append(field); editor.append(wrapper);
    }
    const send = textElement('button', 'Guardar', 'secondary'); send.type = 'submit'; const message = textElement('p', '', 'status');
    editor.append(send, message); editor.addEventListener('submit', async event => {
      event.preventDefault(); send.disabled = true;
      try {
        const data = Object.fromEntries(new FormData(editor));
        for (const name of ['organizations', 'geography']) if (name in data) data[name] = JSON.parse(data[name]);
        for (const name of ['registeringOrganizationId', 'emergencyResponsibleId', 'sourceId']) if (data[name] === '') data[name] = null;
        await api(`/${disaster.id}/${path}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...data, expectedVersion: disaster.version }) });
        await loadDisasters(false);
      } catch (error) { setStatus(message, error.message, true); send.disabled = false; }
    }); details.append(editor); content.append(details);
  }
  const alerts = document.createElement('details'); alerts.append(textElement('summary', 'Alertas y eventos pendientes de entrega'));
  const entries = textElement('pre', ''); alerts.append(entries);
  alerts.addEventListener('toggle', async () => {
    if (!alerts.open) return;
    try { entries.textContent = JSON.stringify(await api(`/${disaster.id}/alertas`), null, 2); }
    catch (error) { entries.textContent = error.message; }
  }); content.append(alerts);
}

const inbox = document.createElement('details'); inbox.append(textElement('summary', 'Reportes recibidos pendientes de asociación'));
const received = textElement('pre', ''); inbox.append(received); document.querySelector('#disaster-list').before(inbox);
inbox.addEventListener('toggle', async () => {
  if (!inbox.open) return;
  try { received.textContent = JSON.stringify(await api('/api/reportes-desastre'), null, 2); }
  catch (error) { received.textContent = error.message; }
});
