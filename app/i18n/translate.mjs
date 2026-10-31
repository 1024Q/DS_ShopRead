// Dictionary-based UI translation. Source strings are the Simplified Chinese texts
// already used throughout the UI, so an untranslated string simply stays Chinese.
// Patterns use numbered/named placeholders: "共 {0} 项" → "{0} items".
const CJK = /[一-鿿]/;
const SEPARATORS = /(\s*[·；]\s*|\s+\/\s+|-)/;
const MEMO_LIMIT = 8000;

function escape(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Character-level converter (Traditional Chinese): `chars` is a string of
// source/target pairs, `phrases` holds the few multi-character exceptions.
// Converting every UI text is far smaller than listing each string.
function converter(chars, phrases = {}) {
  const map = new Map();
  for (let i = 0; i + 1 < chars.length; i += 2) map.set(chars[i], chars[i + 1]);
  const table = new Map(Object.entries(phrases));
  let longest = 1;
  for (const key of table.keys()) longest = Math.max(longest, key.length);
  return (text) => {
    let out = '';
    for (let i = 0; i < text.length;) {
      let hit = false;
      for (let n = Math.min(longest, text.length - i); n >= 2; n--) {
        const value = table.get(text.slice(i, i + n));
        if (value !== undefined) {
          out += value;
          i += n;
          hit = true;
          break;
        }
      }
      if (!hit) {
        out += map.get(text[i]) ?? text[i];
        i++;
      }
    }
    return out;
  };
}

export function compile(dictionary = {}) {
  if (typeof dictionary.chars === 'string')
    return {
      convert: converter(dictionary.chars, dictionary.phrases),
      exact: new Map(),
      patterns: [],
      memo: new Map(),
    };
  const exact = new Map(Object.entries(dictionary.exact || {}));
  const patterns = Object.entries(dictionary.patterns || {})
    .map(([source, target]) => {
      const names = [];
      const literal = source.replace(/\{(\w+)\}/g, '').trim();
      const body = source
        .split(/(\{\w+\})/)
        .map((part) => {
          const name = /^\{(\w+)\}$/.exec(part);
          if (!name) return escape(part);
          names.push(name[1]);
          return '([\\s\\S]+?)';
        })
        .join('');
      return { regex: new RegExp('^' + body + '$'), names, target, weight: literal.length };
    })
    // Prefer the most specific pattern (more literal text).
    .filter((p) => p.weight > 0)
    .sort((a, b) => b.weight - a.weight);
  return { exact, patterns, memo: new Map() };
}

function fill(template, values) {
  return template.replace(/\{(\w+)\}/g, (match, name) => (name in values ? String(values[name]) : match));
}

function lookup(core, compiled, depth = 0) {
  const direct = compiled.exact.get(core);
  if (direct !== undefined) return direct;
  if (depth === 0 && compiled.memo.has(core)) return compiled.memo.get(core);
  let result = null;
  if (core.length <= 400 && depth <= 2) {
    for (const pattern of compiled.patterns) {
      const match = pattern.regex.exec(core);
      if (!match) continue;
      const values = {};
      pattern.names.forEach((name, index) => {
        const value = match[index + 1];
        const trimmed = value.trim();
        // Captured text may itself be a known label, pattern or composite.
        const inner = CJK.test(trimmed) ? lookup(trimmed, compiled, depth + 1) : null;
        values[name] = inner === null || inner === undefined ? value : value.replace(trimmed, inner);
      });
      result = fill(pattern.target, values);
      break;
    }
    if (result === null && SEPARATORS.test(core)) {
      // "A · B；C" — translate each known part, keep the rest.
      const parts = core.split(SEPARATORS);
      let changed = false;
      const out = parts.map((part, index) => {
        if (index % 2) return part;
        const trimmed = part.trim();
        const value = trimmed && CJK.test(trimmed) ? lookup(trimmed, compiled, depth + 1) : null;
        if (value === null || value === undefined) return part;
        changed = true;
        return part.replace(trimmed, value);
      });
      if (changed) result = out.join('');
    }
  }
  if (depth === 0) {
    if (compiled.memo.size > MEMO_LIMIT) compiled.memo.clear();
    compiled.memo.set(core, result);
  }
  return result;
}

/** Translate one text value, keeping its surrounding whitespace. null compiled = source locale. */
export function translateText(text, compiled) {
  if (!compiled || typeof text !== 'string' || !CJK.test(text)) return text;
  const match = /^(\s*)([\s\S]*?)(\s*)$/.exec(text);
  const core = match[2];
  if (compiled.convert) {
    let out = compiled.memo.get(core);
    if (out === undefined) {
      out = compiled.convert(core);
      if (compiled.memo.size > MEMO_LIMIT) compiled.memo.clear();
      compiled.memo.set(core, out);
    }
    return match[1] + out + match[3];
  }
  const value = lookup(core, compiled);
  if (value === null || value === undefined) return text;
  if (value === '') return match[1] || match[3] ? ' ' : '';
  return match[1] + value + match[3];
}

/** t('队列第 {n} 位', { n: 3 }) — explicit API for new code. */
export function translateMessage(source, values, compiled) {
  const template = !compiled
    ? source
    : compiled.convert
      ? compiled.convert(source)
      : (compiled.exact.get(source) ?? source);
  return values ? fill(template, values) : template;
}

export const hasChinese = (text) => CJK.test(text);
