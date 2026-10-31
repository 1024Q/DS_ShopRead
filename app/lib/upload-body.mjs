// Keep File objects as binary; let the browser set the multipart boundary.
export function uploadBody(value) {
  if (!value?.file) return { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) };
  const form = new FormData();
  form.append('file', value.file, value.filename || value.file.name);
  for (const [key, item] of Object.entries(value)) {
    if (!['file', 'filename'].includes(key) && item !== null && item !== undefined) form.append(key, String(item));
  }
  return { body: form };
}
