/* Odile demo chrome — the Webapp's one-shot "software rendering" notice,
 * its permanent "Waking up …" card, its page title and the empty state's
 * accessible name (ODL-330).
 *
 * The browser-hosted Hermes Desktop bridge (browser-bridge-stubs.ts) answers
 * getRemoteDisplayReason() with a fixed string on every boot, so the demo
 * shows a GPU / remote-display warning toast over the first screen for about
 * a second (measured 2026-11-08: present at 0.7 s, gone by 3 s). It is vendor
 * chrome about the server, not our page.
 *
 * CSS cannot select on text, and hiding the whole notification region would
 * also hide real errors, so this hides the one card whose text starts with
 * "Software rendering active". Every other toast is left alone.
 *
 * The same bridge leaves a second, permanent card — a Braille spinner and
 * "Waking up …" — over the empty state because the SPA asks for
 * /api/webapp/socket-ticket, which 404s in v0.23.0 (POST /api/auth/ws-ticket
 * works). It is a false status: the seeded session sends and receives turns.
 * Only that card is hidden; the notifications region is untouched.
 *
 * The stylesheet replaces the vendor wordmark's letters with Odile's; this
 * keeps its accessible name in step, so a screen reader hears the same name
 * a sighted visitor reads.
 *
 * The shell's <title> is the vendor's ("Hermes", and "<screen> · Hermes"
 * once a session is open). BRAND.md §Accessibility tokens asks for the
 * wordmark or the screen name plus the wordmark, so the title is rewritten
 * here at runtime; the shell bytes stay the vendor's, which is what
 * check_demo_host.sh and tests/test_demo_host.py assert.
 *
 * The vendor's active skin (`github`) carries `typography.fontUrl`, and the
 * theme code injects a stylesheet link for it into <head> at runtime, fetching
 * Courier Prime from fonts.googleapis.com. BRAND.md §Typography: no font host
 * is ever contacted. A MutationObserver that removes the link after insertion
 * is too late (measured 2026-11-08 in Chromium: the request has already
 * started, and the resource-timing entry stays), so the insertion itself is
 * patched: any link the vendor marks `data-hermes-theme-font` loses its href
 * before it enters the document. The head observer also removes one that
 * predates this script; the hook is what prevents the request.
 *
 * The session menu is the vendor's own (ODL-391 §2.5 retired 2026-11-14).
 * Delete, Rename… and Archive used to be hidden on the demo while
 * `deploy/demo-host/session_guard.py` refused browser row writes and Caddy
 * refused the REST verbs. Both are gone (t_69f1a5f3, t_ee5aac66): the write
 * reaches Hermes and succeeds, so a hidden control that works is the bug. The
 * script no longer touches the menu — Radix's roving focus, the vendor's
 * separators and the Rename dialog are the whole behaviour.
 *
 * Carried in the image by deploy/image/install-stack.sh §6b, installed to
 * /usr/local/lib/odile/, and copied into apps/desktop/dist-webapp/ as
 * odile-demo-chrome.js by odile-webapp-brand.py, which links it from
 * index.html exactly once. A Webapp rebuild drops both; the same helper (a
 * path unit on the bundle) puts them back. deploy/image/webapp-chrome.js is
 * this file, byte for byte; a test asserts the pair.
 *
 * The image ships this script to every tenant, so the one piece that is the
 * *demo's* and not the product's is gated on `location.hostname`: the
 * empty-state sentence. A subscriber's workspace keeps its own sentence.
 *
 * Two more things follow the machine rather than the demo. The Verify link is
 * the demo's ODL-500 href on demo.odile.fyi and the machine's own `/verify`
 * everywhere else: odile.fyi/verify describes the demo machine, while a
 * subscriber's report is served by that subscriber's VM (odilectl/offboard.py
 * probes the same path). And the workspace gets the semantics the vendor shell
 * omits — `role="main"` on the chat surface and one visually hidden <h1> named
 * as the page title — because BRAND.md §Accessibility tokens asks for one
 * heading per screen and named landmarks.
 */
;(() => {
  'use strict'

  const NEEDLE = 'Software rendering active'
  const WAKING = 'Waking up'
  const REGION = '[aria-label="Notifications"]'
  const WORDMARK = 'p.wordmark'
  const EMPTY = 'p.wordmark + p'
  const EMPTY_SENTENCE = "This is Odile's demo machine: ask it to run whoami and date."
  const COMPOSER = '[data-slot="composer-rich-input"]'
  const COMPOSER_PLACEHOLDER = 'Ask Odile'
  const THEME_FONT = 'link[data-hermes-theme-font]'
  const TITLE = 'Odile'
  const TITLE_SUFFIXES = [' · Hermes Agent', ' · Hermes']
  // The one persistent route from the workspace to the machine's report. It
  // sits in the titlebar, left of the app controls, so a judge meets it on
  // every screen without opening a chat message or a menu (ODL-500). The demo
  // keeps the ODL-500 href; on a subscriber's machine the report is served by
  // that machine at /verify, and odile.fyi/verify describes the demo machine.
  const VERIFY_LINK = 'a.odile-verify-link'
  const VERIFY_HREF_DEMO = 'https://odile.fyi/verify'
  const VERIFY_HREF_TENANT = '/verify'
  const VERIFY_LABEL = 'Verify this machine'
  // The one host where the empty-state sentence and the hidden row writes are
  // correct: demo.odile.fyi is the shared workspace with the judge seed and
  // session_guard.py. A subscriber's machine keeps both.
  const DEMO_HOST = 'demo.odile.fyi'
  const IS_DEMO = location.hostname === DEMO_HOST
  const RIGHT_CLUSTER = '[data-titlebar-cluster="right"]'
  // The workspace's main content and the heading a screen reader starts from
  // (BRAND.md §Accessibility tokens: one <h1> per screen, landmarks named).
  // The vendor shell ships neither; `data-chat-surface` is the stable hook the
  // chat surface itself carries.
  const CHAT_SURFACE = '[data-chat-surface]'
  const WORKSPACE_TITLE = 'h1.odile-workspace-title'
  // The vendor's session menu is left exactly as it ships (ODL-391 §2.5 is
  // retired, 2026-11-14): the demo's `session_guard.py` and Caddy's REST
  // refusal are gone, so Delete, Rename… and Archive work from the judge's
  // browser. `MENU_ITEM` only keeps the scan alive when a menu mounts.
  const MENU_ITEM = '[role="menuitem"]'
  let themeFontGuard = false


  function isThemeFont(node) {
    return Boolean(node && node.nodeType === 1 && node.matches && node.matches(THEME_FONT))
  }

  function stripThemeFont(link) {
    // `alternate` is not a stylesheet, so the browser does not fetch `href`;
    // removing the href is belt and braces for a link whose rel is rewritten
    // back by vendor code before the insert returns.
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
      if ((wordmark.textContent || '').trim() !== 'Odile') {
        wordmark.textContent = 'Odile'
      }
      if (wordmark.getAttribute('aria-label') !== 'Odile') {
        wordmark.setAttribute('aria-label', 'Odile')
      }
    }
  }

  // The empty state's sentence is the one line a judge reads before typing.
  // The vendor rotates its own invitation; this writes Odile's, with the two
  // commands in the brand's mono face. The sentence is a subset of the
  // approved demo row plus the README's first instruction; no claim. It is the
  // demo's sentence, so a subscriber's workspace keeps the vendor's (the
  // composer placeholder below is the product's and is rewritten everywhere).
  function renameEmptyState() {
    const lede = document.querySelector(EMPTY)
    if (IS_DEMO && lede && (lede.textContent || '').trim() !== EMPTY_SENTENCE) {
      const command = (text) => {
        const code = document.createElement('code')
        code.textContent = text
        return code
      }
      lede.replaceChildren(
        document.createTextNode("This is Odile's demo machine: ask it to run "),
        command('whoami'),
        document.createTextNode(' and '),
        command('date'),
        document.createTextNode('.')
      )
    }
    for (const input of document.querySelectorAll(COMPOSER)) {
      if (input.getAttribute('data-placeholder') !== COMPOSER_PLACEHOLDER) {
        input.setAttribute('data-placeholder', COMPOSER_PLACEHOLDER)
      }
    }
  }

  function renameTitle() {
    const current = document.title
    if (!current) {
      return
    }
    // The vendor prefixes an unread count ("(1) Hermes"); keep the count,
    // replace the name. A browser harness may also prefix a horse emoji to
    // every page title; strip it if present.
    const unread = /^\((\d+)\)\s*/.exec(current)
    const prefix = unread ? `(${unread[1]}) ` : ''
    let bare = current.slice(prefix.length).replace(/^🐴\s*/, '')
    let next = bare
    if (bare === 'Hermes' || bare === 'Hermes Agent') {
      next = TITLE
    } else {
      for (const suffix of TITLE_SUFFIXES) {
        if (bare.endsWith(suffix)) {
          next = bare.slice(0, -suffix.length) + ' · ' + TITLE
          break
        }
      }
    }
    next = prefix + next
    if (next !== current) {
      document.title = next
    }
  }

  // A screen reader starts where a sighted visitor does: one level-1 heading
  // named as the page's title, and a main landmark around the workspace. The
  // vendor shell has neither — the empty state's largest word is a <p> and the
  // chat surface is a plain <div> — so the chrome owns both. The heading is
  // visually hidden by the stylesheet and takes no space in the vendor's flex
  // layout; the name is re-read here so a route change keeps heading and
  // title in step. React may re-mount the surface without our nodes; scan()
  // re-applies whatever is missing.
  function ensureLandmarks() {
    const surface = document.querySelector(CHAT_SURFACE)
    if (!surface) {
      return
    }
    if (surface.getAttribute('role') !== 'main') {
      surface.setAttribute('role', 'main')
    }
    let heading = surface.querySelector(WORKSPACE_TITLE)
    if (!heading) {
      heading = document.createElement('h1')
      heading.className = 'odile-workspace-title'
      surface.insertBefore(heading, surface.firstChild)
    }
    const name = document.title || TITLE
    if (heading.textContent !== name) {
      heading.textContent = name
    }
  }

  // The titlebar's right-hand cluster is the app controls. The link is a
  // sibling placed before it, fixed to the same bar; CSS owns the offset and
  // the narrow-width label. The anchor is a real navigation link: it opens
  // the report in the same tab, and the seeded session survives the return.
  function ensureVerifyLink() {
    if (document.querySelector(VERIFY_LINK)) {
      positionVerifyLink()
      return
    }
    const cluster = document.querySelector(RIGHT_CLUSTER)
    if (!cluster || !cluster.parentElement) {
      return
    }
    const link = document.createElement('a')
    link.className = 'odile-verify-link'
    link.href = IS_DEMO ? VERIFY_HREF_DEMO : VERIFY_HREF_TENANT
    link.setAttribute('aria-label', VERIFY_LABEL)
    link.appendChild(document.createTextNode('Verify'))
    const more = document.createElement('span')
    more.className = 'odile-verify-more'
    more.textContent = ' this machine'
    link.appendChild(more)
    const arrow = document.createElement('span')
    arrow.className = 'odile-verify-arrow'
    arrow.setAttribute('aria-hidden', 'true')
    arrow.textContent = ' →'
    link.appendChild(arrow)
    cluster.parentElement.insertBefore(link, cluster)
    positionVerifyLink()
  }

  // The app-controls cluster is 48 x 24 px (two 24 px controls) at both
  // widths; anchor the link to the cluster's real box so it keeps an 8 px gap
  // and a 44 px hit area. The box starts 5 px below the viewport top so the
  // sheet's 2 px ring at a 3 px offset is whole, and the label is centred on
  // the cluster's own line by trimming the box's bottom padding to twice the
  // distance from the box's centre to the cluster's centre.
  function positionVerifyLink() {
    const link = document.querySelector(VERIFY_LINK)
    const cluster = document.querySelector(RIGHT_CLUSTER)
    if (!link || !cluster) {
      return
    }
    const box = cluster.getBoundingClientRect()
    const top = 5
    const height = 44
    link.style.right = `${Math.max(8, window.innerWidth - box.left + 8)}px`
    link.style.top = `${top}px`
    link.style.height = `${height}px`
    const centre = box.top + box.height / 2
    const padding = Math.round(height - 2 * (centre - top))
    link.style.paddingBottom = `${Math.max(0, Math.min(height - 12, padding))}px`
  }

  function scan() {
    const region = document.querySelector(REGION)
    if (region) {
      hideNotice(region)
    }
    hideWaking()
    renameWordmark()
    renameEmptyState()
    renameTitle()
    ensureLandmarks()
    ensureVerifyLink()
  }

  function touchesChrome(node) {
    if (!node || node.nodeType !== 1) {
      return false
    }
    if (
      typeof node.matches === 'function' &&
      (node.matches(REGION) ||
        node.matches(WORDMARK) ||
        node.matches(EMPTY) ||
        node.matches(COMPOSER) ||
        node.matches(MENU_ITEM) ||
        node.matches(VERIFY_LINK) ||
        node.matches(CHAT_SURFACE) ||
        node.matches(RIGHT_CLUSTER))
    ) {
      return true
    }
    if (typeof node.closest === 'function' && (node.closest(REGION) || node.closest(WORDMARK))) {
      return true
    }
    if (node.textContent && node.textContent.includes(WAKING)) {
      return true
    }
    return (
      typeof node.querySelector === 'function' &&
      Boolean(
        node.querySelector(REGION) ||
          node.querySelector(WORDMARK) ||
          node.querySelector(COMPOSER) ||
          node.querySelector(MENU_ITEM) ||
          node.querySelector(CHAT_SURFACE) ||
          node.querySelector(RIGHT_CLUSTER)
      )
    )
  }

  function watchHead() {
    new MutationObserver(() => {
      renameTitle()
      ensureLandmarks()
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
    window.addEventListener('resize', positionVerifyLink)
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        // The vendor re-renders the composer and overwrites its placeholder on
        // every mount ("What should we tackle?", "Give Hermes a task"), so the
        // owned copy has to be re-applied at the attribute, not just once at
        // start-up. Writing the same value back is a no-op on the next pass.
        if (
          record.type === 'attributes' &&
          record.target.matches &&
          record.target.matches(COMPOSER) &&
          record.target.getAttribute('data-placeholder') !== COMPOSER_PLACEHOLDER
        ) {
          record.target.setAttribute('data-placeholder', COMPOSER_PLACEHOLDER)
          continue
        }
        if (
          record.type === 'characterData' &&
          record.target.parentElement &&
          (record.target.parentElement.closest(WORDMARK) ||
            record.target.parentElement.matches(EMPTY))
        ) {
          scan()
          return
        }
        // A wholesale textContent swap arrives as a childList on the <p>, with
        // only text nodes in addedNodes; re-apply the owned sentence there too.
        if (
          record.type === 'childList' &&
          record.target &&
          record.target.nodeType === 1 &&
          (record.target.matches(WORDMARK) || record.target.matches(EMPTY))
        ) {
          scan()
          return
        }
        // The app controls cluster is re-mounted on some layouts; when the
        // verify link goes with it, put it back before the next paint.
        for (const node of record.removedNodes) {
          if (
            node &&
            node.nodeType === 1 &&
            (node.matches(VERIFY_LINK) || node.querySelector(VERIFY_LINK))
          ) {
            scan()
            return
          }
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
      attributeFilter: ['data-placeholder'],
    })
  }

  // The guard and the head observer run now, at this script's parse time
  // (the tag is not deferred, so this precedes the vendor's module graph);
  // start() waits for the body because it scans chrome the app mounts
  // later. On the live demo (2026-11-08) the theme font link is appended at
  // ~1.56 s, after this point, so the guard sees it before the fetch.
  guardThemeFonts()
  watchHead()

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true })
  } else {
    start()
  }
})()
