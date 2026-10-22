"""Reusable, standalone synchronous Playwright components and toast assertions.

This module is copied into exports; it has no dependency on the recording server.
"""
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class Component:
    def __init__(self, definition):
        self.definition = definition

    def scope(self, page):
        scope = page
        for selector in self.definition['locator'].get('frames', []):
            element = scope.locator('css=' + selector).element_handle()
            scope = element.content_frame() if element else None
            if scope is None:
                raise AssertionError(f"iframe 未就绪：{selector}")
        return scope

    def locator(self, page):
        return self.scope(page).locator('css=' + self.definition['locator']['css'])

    def click(self, page, button='left', modifiers=None):
        self.locator(page).click(button=button, modifiers=modifiers or [])

    def dblclick(self, page, button='left', modifiers=None):
        self.locator(page).dblclick(button=button, modifiers=modifiers or [])

    def hover(self, page):
        self.locator(page).hover()

    def press(self, page, key):
        self.locator(page).press(key)

    def wheel(self, page, dx, dy):
        self.hover(page)
        page.mouse.wheel(dx, dy)

    def drag_to(self, page, destination):
        self.locator(page).drag_to(destination.locator(page))


class Button(Component):
    pass


class TextInput(Component):
    def fill(self, page, value):
        self.locator(page).fill(value)


class Checkbox(Component):
    def check(self, page, checked):
        self.locator(page).set_checked(checked)


class Select(Component):
    def select(self, page, value):
        self.locator(page).select_option(value=value)


class Frame(Component):
    def content(self, page):
        return self.locator(page).content_frame


class ComponentLibrary:
    TYPES = {'button':Button, 'input':TextInput, 'checkbox':Checkbox, 'select':Select, 'frame':Frame}

    def __init__(self, path_or_definitions):
        self.definitions = (json.loads(Path(path_or_definitions).read_text('utf-8'))
                            if isinstance(path_or_definitions, (str, Path)) else path_or_definitions)
        self._cache = {}

    def get(self, key):
        if key not in self._cache:
            definition = self.definitions[key]
            self._cache[key] = self.TYPES.get(definition['kind'], Component)(definition)
        return self._cache[key]

    def button(self, key):
        return self.get(key)

    def input(self, key):
        return self.get(key)

    def frame(self, key):
        return self.get(key)


@dataclass(frozen=True)
class ToastExpectation:
    component: str
    text: Optional[str] = None


_ARM = r"""({id, selectors}) => {
  window.__wttAssertions ||= {};
  const state = {found: [], previous: new WeakMap()};
  const scan = (baseline=false) => {
    for (const css of selectors) {
      for (const el of document.querySelectorAll(css)) {
        const visible = !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        if (!visible) { state.previous.delete(el); continue; }
        const text = (el.textContent || '').trim();
        if (!baseline && state.previous.get(el) !== text) state.found.push({css, text});
        state.previous.set(el, text);
      }
    }
  };
  scan(true);
  state.observer = new MutationObserver(() => scan());
  state.observer.observe(document.documentElement, {subtree:true,childList:true,attributes:true,characterData:true});
  state.timer = setInterval(() => scan(), 25);
  window.__wttAssertions[id] = state;
}"""


class ToastAssertions:
    def __init__(self, components):
        self.components = components

    def assert_visible(self, page, expected, timeout_ms=5000):
        locator = self.components.get(expected.component).locator(page)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            texts = locator.evaluate_all('''nodes => nodes.filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
                .map(el => (el.textContent || '').trim())''')
            if texts and (expected.text is None or expected.text in texts):
                return
            if time.monotonic() >= deadline:
                raise AssertionError(f'未出现预期 toast：{expected!r}')
            page.wait_for_timeout(25)

    def assert_after_click(self, page, click, expected, timeout_ms=5000):
        """Arm before click and remember even a toast removed before click returns.

        Text is only assertion data. All locating uses the stored CSS hierarchy.
        expected may contain multiple toasts, including ones inside child frames.
        """
        if isinstance(expected, ToastExpectation):
            expected = [expected]
        if not expected:
            return click()
        watches = []
        try:
            for item in expected:
                component = self.components.get(item.component)
                scope, watch_id = component.scope(page), uuid.uuid4().hex
                css = component.definition['locator']['css']
                scope.evaluate(_ARM, {'id':watch_id, 'selectors':[css]})
                watches.append((scope, watch_id, item))
            result = click()
            deadline = time.monotonic() + timeout_ms / 1000
            while True:
                missing = []
                for scope, watch_id, item in watches:
                    found = scope.evaluate('(id) => window.__wttAssertions?.[id]?.found || []', watch_id)
                    if not any(item.text is None or row['text'] == item.text for row in found):
                        missing.append(item)
                if not missing:
                    return result
                if time.monotonic() >= deadline:
                    raise AssertionError(f'点击后未出现预期 toast：{missing!r}')
                page.wait_for_timeout(25)
        finally:
            for scope, watch_id, _ in watches:
                try:
                    scope.evaluate('''id => {
                      const s = window.__wttAssertions?.[id];
                      if(s) { s.observer.disconnect(); clearInterval(s.timer); delete window.__wttAssertions[id]; }
                    }''', watch_id)
                except Exception:
                    pass


def wait_for_new_page(context, known, timeout_ms=10000):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for page in context.pages:
            if page not in known:
                return page
        if not context.pages:
            break
        context.pages[0].wait_for_timeout(25)
    raise AssertionError('录制中的新窗口未出现')
