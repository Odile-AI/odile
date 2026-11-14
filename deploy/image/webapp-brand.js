/* Odile Webapp brand — the names and the credit a skin cannot reach (ODL-240).
 *
 * The Webapp is Hermes's desktop UI. A Hermes skin (skins/odile.yaml) themes
 * the palette, the custom CSS and the agent name for the CLI/TUI, but v0.23.0
 * still hard-codes four user-visible strings in the bundle:
 *
 *   <title>                 "Hermes", and "<screen> · Hermes" once a session opens
 *   p.wordmark              "HERMES AGENT" on the empty chat
 *   composer placeholder    "Give Hermes a task", from a rotating prompt list
 *   About > Version         the vendor hero "Hermes" (kept: it is the credit)
 *
 * and the About panel carries no "Powered by Hermes" line at all. This script
 * is installed by the installer into the same-origin bundle and linked from
 * the shell — no bundle fork, one same-origin request.
 *
 * It also sets the first-visit appearance to the Odile skin. The desktop
 * resolves a stored skin name against the gateway's skin registry only after
 * `gateway.ready` seeds it, so a fresh browser (no localStorage) would
 * otherwise paint the bundle's default until the user picked a skin by hand.
 * The write happens only when the browser has no saved appearance at all; a
 * later choice by the owner wins and is never overwritten. Until the registry
 * arrives, an !important stylesheet holds Odile's paper and ink over the
 * bundle's blue, then removes itself when the real theme is written.
 *
 * Two false status surfaces the browser bridge leaves over the first screen
 * are hidden by text: the one-second "Software rendering active" toast and the
 * permanent "Waking up …" card (the SPA asks for /api/webapp/socket-ticket,
 * which 404s in v0.23.0). Only those nodes are hidden; the notifications
 * region and real errors are untouched.
 *
 * The vendor's active skin can carry `typography.fontUrl`, and the theme code
 * injects a stylesheet link for it at runtime. BRAND.md §Typography: no font
 * host is ever contacted. Removing the link after insertion is too late (the
 * request has started), so the insertion itself is intercepted: a link marked
 * `data-hermes-theme-font` loses its href before it reaches <head>.
 *
 * Installed by installer/odile_install.py (ensure_webapp_brand); the tag is
 * parse-blocking so the font guard and the first-visit key run before the
 * vendor's module graph. A Webapp rebuild drops the tag; the next installer
 * run puts it back.
 */
;(() => {
  'use strict'

  const NAME = 'Odile'
  const THEME_KEY = 'hermes-desktop-theme-v2'
  const PROFILE_THEMES_KEY = 'hermes-desktop-profile-themes-v1'
  const POWERED_BY = 'Powered by Hermes'
  const NEEDLE = 'Software rendering active'
  const WAKING = 'Waking up'
  const REGION = '[aria-label="Notifications"]'
  const WORDMARK = 'p.wordmark'
  const PLACEHOLDER_ATTRS = ['data-placeholder', 'placeholder']
  const PLACEHOLDER = PLACEHOLDER_ATTRS.map((attr) => '[' + attr + ']').join(', ')
  const THEME_FONT = 'link[data-hermes-theme-font]'
  const TITLE_SUFFIXES = [' · Hermes Agent', ' · Hermes']
  const EARLY_STYLE = 'odile-early-paint'
  let themeFontGuard = false

  // True when this browser already holds an appearance: a global theme key or
  // a per-profile one. On a first visit both are absent and the Odile skin is
  // the default (see chooseDefaultSkin).
  function hasSavedAppearance() {
    try {
      if (localStorage.getItem(THEME_KEY)) {
        return true
      }
      const profiles = JSON.parse(localStorage.getItem(PROFILE_THEMES_KEY) || '{}')
      return Boolean(profiles && typeof profiles === 'object' && Object.keys(profiles).length)
    } catch (_) {
      // Storage unavailable (private mode / quota): treat it as a first visit.
      // The early guard only paints Odile's own palette, so it is harmless.
      return false
    }
  }

  // First visit: pick Odile, but never overwrite an appearance the owner has
  // already chosen (globally or per profile).
  function chooseDefaultSkin() {
    if (hasSavedAppearance()) {
      return
    }
    try {
      localStorage.setItem(THEME_KEY, 'odile')
      // The shell's pre-paint script reads these before the bundle runs; a
      // first visit should open on paper, not a white frame that turns paper
      // a second later. applyTheme() keeps both keys fresh after this.
      localStorage.setItem('hermes-boot-background', '#F6F0E4')
      localStorage.setItem('hermes-boot-color-scheme', 'light')
    } catch (_) {
      // Storage unavailable (private mode / quota): the gateway still offers
      // the skin in Appearance; nothing else depends on the key.
    }
  }

  // First paint: until `gateway.ready` resolves a skin name against the
  // gateway registry, the bundle paints its own palette — a blue accent on the
  // sidebar labels and the connection state, about two seconds on a first
  // visit. Hold the settled values in an !important stylesheet so the first
  // frame is already Odile, then drop the sheet as soon as applyTheme() writes
  // its own inline `--theme-primary`. After that the owner's choice wins.
  function guardFirstPaint() {
    if (hasSavedAppearance()) {
      return
    }
    const root = document.documentElement
    if (!root || !document.head) {
      return
    }
    const style = document.createElement('style')
    style.id = EARLY_STYLE
    style.textContent =
      ':root{' +
      '--theme-primary:#1C1813 !important;' +
      '--theme-foreground:#1C1813 !important;' +
      '--theme-background-seed:#F6F0E4 !important;' +
      '--theme-midground:#1C1813 !important}'
    document.head.appendChild(style)
    let observer = null
    let timer = 0
    const release = () => {
      // The bundle's own boot writes #0053fd before any skin resolves; that is
      // not a theme. Wait for the resolved value applyTheme() writes.
      const value = root.style.getPropertyValue('--theme-primary').trim()
      if (!value || value.toLowerCase() === '#0053fd') {
        return
      }
      style.remove()
      if (observer) {
        observer.disconnect()
      }
      clearTimeout(timer)
    }
    observer = new MutationObserver(release)
    observer.observe(root, { attributes: true, attributeFilter: ['style'] })
    // A skin that never resolves must not pin the palette forever.
    timer = setTimeout(() => {
      style.remove()
      observer.disconnect()
    }, 20000)
    release()
  }

  function isThemeFont(node) {
    return Boolean(node && node.nodeType === 1 && node.matches && node.matches(THEME_FONT))
  }

  function stripThemeFont(link) {
    link.removeAttribute('href')
    link.setAttribute('rel', 'alternate')
  }

  function stripThemeFonts() {
    for (const link of document.querySelectorAll(THEME_FONT)) {
      stripThemeFont(link)
      link.remove()
    }
  }

  function guardThemeFonts() {
    if (themeFontGuard || !document.head) {
      return
    }
    themeFontGuard = true
    const head = document.head
    const wrap = (name) => {
      const original = head[name]
      if (typeof original !== 'function') {
        return
      }
      head[name] = function (...nodes) {
        for (const node of nodes) {
          if (isThemeFont(node)) {
            stripThemeFont(node)
          }
        }
        return original.apply(this, nodes)
      }
    }
    for (const name of ['appendChild', 'insertBefore', 'replaceChild', 'append', 'prepend', 'replaceChildren']) {
      wrap(name)
    }
  }

  function hideNotice(region) {
    for (const card of region.children) {
      if (card.textContent && card.textContent.includes(NEEDLE)) {
        card.style.display = 'none'
      }
    }
  }

  function hideWaking() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    let node
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !node.nodeValue.includes(WAKING)) {
        continue
      }
      const card = node.parentElement
      if (card && card.tagName === 'DIV' && card.classList.contains('font-mono')) {
        card.style.display = 'none'
      }
    }
  }

  function renameWordmark() {
    for (const wordmark of document.querySelectorAll(WORDMARK)) {
      if ((wordmark.textContent || '').trim() !== NAME) {
        wordmark.textContent = NAME
      }
      if (wordmark.getAttribute('aria-label') !== NAME) {
        wordmark.setAttribute('aria-label', NAME)
      }
    }
  }

  // The new-session composer rotates its prompt from a list that includes
  // "Give Hermes a task". The prompt lives in an attribute and is painted by
  // the bundle's `:empty::before`; a skin cannot reach either, so any value
  // that still names the vendor agent is renamed in place. The bundle may
  // rewrite the attribute when the composer remounts, which is why the
  // observer also watches these two attributes.
  function renamePlaceholders() {
    for (const node of document.querySelectorAll(PLACEHOLDER)) {
      for (const attr of PLACEHOLDER_ATTRS) {
        const value = node.getAttribute(attr)
        if (value && value.includes('Hermes')) {
          node.setAttribute(attr, value.split('Hermes').join(NAME))
        }
      }
    }
  }

  function renameTitle() {
    const current = document.title
    if (!current || current === NAME) {
      return
    }
    let next = current
    if (current === 'Hermes' || current === 'Hermes Agent') {
      next = NAME
    } else {
      for (const suffix of TITLE_SUFFIXES) {
        if (current.endsWith(suffix)) {
          next = current.slice(0, -suffix.length) + ' · ' + NAME
          break
        }
      }
    }
    if (next !== current) {
      document.title = next
    }
  }

  // About > Version & updates: after "Version <x.y.z>", add the credit line
  // BRAND.md asks for. The vendor hero "Hermes" stays: About is where the
  // open-source core is named.
  function addPoweredBy() {
    const hero = Array.from(document.querySelectorAll('h2')).find(
      (h) =>
        (h.textContent || '').trim() === 'Hermes' &&
        h.nextElementSibling &&
        /^Version\b/.test((h.nextElementSibling.textContent || '').trim())
    )
    if (!hero) {
      return
    }
    const host = hero.parentElement
    if (!host || host.querySelector('.odile-powered-by')) {
      return
    }
    const line = document.createElement('p')
    line.className = 'odile-powered-by'
    line.textContent = POWERED_BY
    hero.nextElementSibling.insertAdjacentElement('afterend', line)
  }

  function scan() {
    const region = document.querySelector(REGION)
    if (region) {
      hideNotice(region)
    }
    hideWaking()
    renameWordmark()
    renamePlaceholders()
    renameTitle()
    addPoweredBy()
  }

  function touchesChrome(node) {
    if (!node || node.nodeType !== 1) {
      return false
    }
    if (typeof node.closest === 'function' && (node.closest(REGION) || node.closest(WORDMARK))) {
      return true
    }
    if (
      typeof node.matches === 'function' &&
      (node.matches(REGION) || node.matches(WORDMARK) || node.matches(PLACEHOLDER))
    ) {
      return true
    }
    if (node.textContent && (node.textContent.includes(WAKING) || node.textContent.includes('Version'))) {
      return true
    }
    return (
      typeof node.querySelector === 'function' &&
      Boolean(node.querySelector(REGION) || node.querySelector(WORDMARK) || node.querySelector(PLACEHOLDER))
    )
  }

  function watchHead() {
    new MutationObserver(() => {
      renameTitle()
      stripThemeFonts()
    }).observe(document.head, {
      childList: true,
      subtree: true,
      characterData: true,
    })
  }

  function start() {
    scan()
    stripThemeFonts()
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === 'attributes') {
          // The composer rewrote its prompt; rename it before it paints.
          if (record.attributeName && PLACEHOLDER_ATTRS.includes(record.attributeName)) {
            renamePlaceholders()
          }
          continue
        }
        if (
          record.type === 'characterData' &&
          record.target.parentElement &&
          record.target.parentElement.closest(WORDMARK)
        ) {
          scan()
          return
        }
        for (const node of record.addedNodes) {
          if (touchesChrome(node)) {
            scan()
            return
          }
        }
      }
    })
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: PLACEHOLDER_ATTRS,
    })
  }

  guardFirstPaint()
  chooseDefaultSkin()
  guardThemeFonts()
  watchHead()

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true })
  } else {
    start()
  }
})()
