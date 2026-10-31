async (characters, family = 'spider-font') => {
  await document.fonts.load(`128px "${family}"`, characters.join(''));
  const canvas = document.createElement('canvas');
  canvas.width = 180;
  canvas.height = 180;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const output = {};
  for (const character of characters) {
    ctx.clearRect(0, 0, 180, 180);
    ctx.font = `128px "${family}"`;
    ctx.fillStyle = '#000';
    ctx.fillText(character, 10, 140);
    const pixels = ctx.getImageData(0, 0, 180, 180).data;
    let left = 180,
      top = 180,
      right = 0,
      bottom = 0;
    for (let y = 0; y < 180; y++)
      for (let x = 0; x < 180; x++)
        if (pixels[(y * 180 + x) * 4 + 3] > 127) {
          left = Math.min(left, x);
          right = Math.max(right, x);
          top = Math.min(top, y);
          bottom = Math.max(bottom, y);
        }
    if (left > right) throw Error('数字字体未显示');
    const small = document.createElement('canvas');
    small.width = 32;
    small.height = 48;
    const target = small.getContext('2d', { willReadFrequently: true });
    target.drawImage(canvas, left, top, right - left + 1, bottom - top + 1, 0, 0, 32, 48);
    const bits = target.getImageData(0, 0, 32, 48).data;
    output[character] = Array.from({ length: 48 }, (_, y) => {
      let row = 0;
      for (let x = 0; x < 32; x++) if (bits[(y * 32 + x) * 4 + 3] > 127) row = (row | (1 << x)) >>> 0;
      return row.toString(16).padStart(8, '0');
    }).join('');
  }
  return output;
};
