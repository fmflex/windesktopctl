# Third-party notices

## Windows-MCP (design reference)

windesktopctl v2 borrows design ideas from
[Windows-MCP](https://github.com/CursorTouch/Windows-MCP) (shallow clone at
commit `ca70cf7`): the UIA `CacheRequest` property list and
`FindAllBuildCache` traversal approach (`tree/cache_utils.py`), DOM-mode
subtree rooting on `AutomationId=="RootWebArea"` / first `Document`
(`tree/service.py`), password-field value suppression, low-level
`WH_MOUSE_LL`/`WH_KEYBOARD_LL` takeover hooks with injected-event filtering
(`desktop/control.py`, `desktop/control_hooks.py`), and WaitFor-style
condition polling (`tools/input.py`).

The implementations here are rewritten/simplified for windesktopctl's architecture
(single COM worker thread, compact text output); no source files were copied
verbatim.

```
MIT License

Copyright (c) 2025 JEOMON GEORGE

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
