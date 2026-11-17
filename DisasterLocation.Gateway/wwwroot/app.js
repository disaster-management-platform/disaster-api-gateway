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
  for (const field of ['nombre', 'ubicacion', 'severidad', 'descripcion', 'latitud', 'longitud', 'radio'])
    form.elements.namedItem(field).value = disaster[field] ?? '';
}
try {
  pendingSubmission = JSON.parse(sessionStorage.getItem(submissionStorage) ?? 'null');
  if (pendingSubmission) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(pendingSubmission.key))
      throw new Error('Identidad pendiente inválida.');
    restoreSubmission();
    retryButton.hidden = false;
    setStatus(formStatus, 'Hay un envío sin confirmar. Reinténtalo antes de iniciar otra operación.');
  }
} catch {
  submitButton.disabled = true;
  setStatus(formStatus, 'No se pudo recuperar la operación pendiente. Revisa el almacenamiento de esta pestaña antes de continuar.', true);
}
retryButton.addEventListener('click', () => {
  restoreSubmission();
  updateSelection();
  form.requestSubmit();
});
const colors = ['#27815b', '#738520', '#cb9223', '#d16b30', '#b83d46'];
let map;
let markers;
let selection;

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
  const response = await (window.platformFetch ?? window.fetch)(`/api/desastres${path}`, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    const messages = problem?.errors ? Object.values(problem.errors).flat().join(' ') : null;
    throw Object.assign(new Error(messages || `No se pudo completar la solicitud (HTTP ${response.status}). Comprueba que la API esté ejecutándose.`), { status: response.status });
  }
  return response.json();
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
  const data = new FormData(form);
  const disaster = {
    nombre: data.get('nombre'), ubicacion: data.get('ubicacion'),
    severidad: Number(data.get('severidad')), descripcion: data.get('descripcion'),
    latitud: Number(data.get('latitud')), longitud: Number(data.get('longitud')), radio
  };
  const content = JSON.stringify(disaster);
  if (pendingSubmission && pendingSubmission.content !== content) {
    setStatus(formStatus, 'El envío anterior sigue sin confirmar. Usa «Reintentar el envío pendiente» antes de iniciar otra operación.', true);
    return;
  }
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
    if (error.status === 400) {
      sessionStorage.removeItem(submissionStorage);
      pendingSubmission = undefined;
    }
    retryButton.hidden = !pendingSubmission;
    setStatus(formStatus, error.message, true);
  } finally {
    submitButton.disabled = false;
    retryButton.disabled = false;
  }
});

refreshButton.addEventListener('click', () => loadDisasters());
loadDisasters();
