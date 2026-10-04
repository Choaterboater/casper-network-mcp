# Third-party notices

casper-network-mcp is MIT licensed (`LICENSE`). Each copied or adapted file says where it came
from at the top; this file says where that code comes from and keeps the licence notices of the MIT projects whose
material is shipped.

## hpe-networking-mcp, and nowireless4u/hpe-networking-mcp as a reference

The router (`router/`), the generated-tool runtime (`openapi_gen/`), the shared helpers in `core/` (annotations,
redaction, safe paths, response budget and cursors, HTTP client, server transport), the bundled-spec lookup
(`specs_index.py`, `products/specs_tools.py`) and the Central, Mist and ClearPass hand-written tools are adapted
from hpe-networking-mcp, an earlier server by this project's author. That code is the author's own and is covered by
this project's `LICENSE`.

hpe-networking-mcp in turn used [nowireless4u/hpe-networking-mcp](https://github.com/nowireless4u/hpe-networking-mcp),
an MIT-licensed community project, as reference material (its generated-tool architecture and platform coverage were
compared against it) while keeping its own clients, registration, routing and response handling. No code from that
project is copied here; it is credited because it shaped the design.

## mistsys/mist_openapi

Source: <https://github.com/mistsys/mist_openapi> (MIT).

`src/casper_network_mcp/specs/mist.openapi.json` is that repository's OpenAPI document, byte for byte, at the
commit pinned in `scripts/spec_pins.json`; the generated Mist tools and the Mist part of find_tool's index are built
from it. Its licence:

```
MIT License

Copyright (c) 2020 Thomas Munzer

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

## HPE Aruba Networking API documents

Not MIT and not open source. See `NOTICE.md` (sections A and C) for what they are and on what basis they are
redistributed.
