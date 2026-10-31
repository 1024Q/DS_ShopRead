'use client';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { compile, translateMessage, translateText } from './translate.mjs';
import { DEFAULT_LOCALE, STORAGE_KEY, normalizeLocale } from './locales.mjs';

// Dictionaries load on demand so the default Simplified Chinese bundle stays small.
const loaders = {
  en: () => import('./en.mjs'),
  'zh-TW': () => import('./zh-TW.mjs'),
};
const compiledCache = {};
async function load(locale) {
  if (locale === DEFAULT_LOCALE) return null;
  if (!compiledCache[locale]) compiledCache[locale] = loaders[locale]().then((m) => compile(m.default));
  return compiledCache[locale];
}

const I18nContext = createContext({
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
  t: (source, values) => translateMessage(source, values, null),
});

export const useI18n = () => useContext(I18nContext);

// ---- DOM bridge -----------------------------------------------------------
// Existing screens render hard-coded Simplified Chinese. Instead of rewriting
// every component at once, text nodes and a few attributes are translated after
// React renders them. Each node remembers its source text, so React updates and
// switching back to Simplified Chinese restore the original. Elements marked
// translate="no" or .notranslate (user data such as shop names) are skipped.
const ATTRIBUTES = ['placeholder', 'title', 'aria-label'];
const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'CODE', 'PRE', 'NOSCRIPT']);

function skipped(element, attribute = false) {
  for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
    if (!attribute && (SKIP_TAGS.has(node.tagName.toUpperCase()) || node.isContentEditable)) return true;
    if (node.getAttribute('translate') === 'no' || node.classList?.contains('notranslate')) return true;
  }
  return false;
}

function createBridge() {
  const texts = new WeakMap();
  const attrs = new WeakMap();
  let compiled = null;
  let observer = null;
  let headObserver = null;
  let titleSource = null;

  function text(node) {
    const current = node.nodeValue;
    const record = texts.get(node);
    if (record && current === record.out) return;
    if (!compiled) {
      if (record) texts.delete(node);
      return;
    }
    if (skipped(node.parentElement)) return;
    const out = translateText(current, compiled);
    texts.set(node, { src: current, out });
    if (out !== current) node.nodeValue = out;
  }
  function attribute(element, name) {
    const value = element.getAttribute(name);
    if (value == null) return;
    const map = attrs.get(element) || {};
    const record = map[name];
    if (record && value === record.out) return;
    if (!compiled || skipped(element, true)) return;
    const out = translateText(value, compiled);
    map[name] = { src: value, out };
    attrs.set(element, map);
    if (out !== value) element.setAttribute(name, out);
  }
  function walk(root) {
    if (root.nodeType === 3) return text(root);
    if (root.nodeType !== 1 && root.nodeType !== 11) return;
    if (root.nodeType === 1) for (const name of ATTRIBUTES) if (root.hasAttribute(name)) attribute(root, name);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeType === 3) text(node);
      else for (const name of ATTRIBUTES) if (node.hasAttribute(name)) attribute(node, name);
    }
  }
  function restore(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeType === 3) {
        const record = texts.get(node);
        if (record && node.nodeValue === record.out) node.nodeValue = record.src;
        texts.delete(node);
      } else {
        const map = attrs.get(node);
        if (!map) continue;
        for (const [name, record] of Object.entries(map))
          if (node.getAttribute(name) === record.out) node.setAttribute(name, record.src);
        attrs.delete(node);
      }
    }
  }
  function title() {
    if (titleSource === null || (document.title && document.title !== titleSource.out))
      titleSource = { src: document.title, out: document.title };
    const out = compiled ? translateText(titleSource.src, compiled) : titleSource.src;
    titleSource.out = out;
    if (document.title !== out) document.title = out;
  }
  return {
    apply(next) {
      compiled = next;
      if (!compiled) {
        observer?.disconnect();
        headObserver?.disconnect();
        observer = null;
        headObserver = null;
        restore(document.body);
        title();
        return;
      }
      walk(document.body);
      title();
      if (observer) return;
      // Next.js may re-render <title> after hydration; keep it translated too.
      headObserver = new MutationObserver(() => title());
      headObserver.observe(document.head, { subtree: true, childList: true, characterData: true });
      observer = new MutationObserver((records) => {
        for (const record of records) {
          if (record.type === 'characterData') text(record.target);
          else if (record.type === 'attributes') attribute(record.target, record.attributeName);
          else for (const node of record.addedNodes) walk(node);
        }
      });
      observer.observe(document.body, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ATTRIBUTES,
      });
    },
    stop() {
      observer?.disconnect();
      headObserver?.disconnect();
      observer = null;
      headObserver = null;
    },
  };
}

export default function I18nProvider({ children }) {
  const [locale, setLocaleState] = useState(DEFAULT_LOCALE);
  const [compiled, setCompiled] = useState(null);
  const bridge = useRef(null);
  useEffect(() => {
    let saved = null;
    try {
      saved = localStorage.getItem(STORAGE_KEY);
    } catch {}
    if (saved) setLocaleState(normalizeLocale(saved));
  }, []);
  useEffect(() => {
    let active = true;
    document.documentElement.lang = locale;
    load(locale)
      .then((value) => {
        if (!active) return;
        setCompiled(value);
        if (!bridge.current) bridge.current = createBridge();
        bridge.current.apply(value);
      })
      .catch(() => {
        // A missing dictionary must never break the console: stay in Simplified Chinese.
        if (active) setCompiled(null);
      });
    return () => {
      active = false;
    };
  }, [locale]);
  useEffect(() => () => bridge.current?.stop(), []);
  const setLocale = useCallback((value) => {
    const next = normalizeLocale(value);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {}
    setLocaleState(next);
  }, []);
  const t = useCallback((source, values) => translateMessage(source, values, compiled), [compiled]);
  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
