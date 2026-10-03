# Third-party notices

Iron Owl is built on free and open-source software. This file lists what the installed app
contains that other people wrote, with each license. It is installed with the app
(`%LOCALAPPDATA%\Programs\FinTrack\THIRD_PARTY_NOTICES.md`).

Iron Owl's own license is in `LICENSE`.

## Python

| Component | Version | License |
|---|---|---|
| CPython (the official Windows "embeddable package") | 3.11.9 | Python Software Foundation License Version 2 (PSF-2.0) |

Copyright (c) 2001 Python Software Foundation; All Rights Reserved. The full license, and the
licenses of the libraries Python itself includes (such as OpenSSL, libffi, SQLite, zlib, bzip2
and xz), are in `python\LICENSE.txt` in the install folder and at
<https://docs.python.org/3.11/license.html>.

## Python packages (the app's runtime)

These are installed into `runtimes\<id>\site-packages` exactly as published on PyPI (wheels,
pinned in `backend/requirements.txt` and hash-checked by `tools/release/requirements-lock.txt`).
plaid-python publishes only its source, so the build turns that exact, hash-checked source into
a wheel, unchanged. Each package's own license file ships beside it, in its `*.dist-info` folder.

| Package | Version | License |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 46.0.7 | Apache-2.0 OR BSD-3-Clause (its Windows wheel includes OpenSSL, Apache-2.0) |
| fastapi | 0.141.1 | MIT |
| h11 | 0.16.0 | MIT |
| httptools | 0.8.0 | MIT |
| idna | 3.20 | BSD-3-Clause |
| nulltype | 2.3.1 | Apache-2.0 |
| plaid-python | 44.0.0 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| python-dateutil | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause (dual) |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| six | 1.17.0 | MIT |
| SQLAlchemy | 2.1.1 | MIT |
| sqlcipher3-wheels | 0.5.7 | zlib/libpng (includes SQLCipher, BSD-style, Copyright (c) Zetetic LLC; and OpenSSL, Apache-2.0) |
| starlette | 1.7.0 | BSD-3-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| watchfiles | 1.3.0 | MIT |
| websockets | 17.1 | BSD-3-Clause |

## In the app window (the web page)

These JavaScript libraries are bundled into the app's web files (`versions\<v>\web`).

| Package | Version | License | Copyright |
|---|---|---|---|
| react, react-dom, scheduler | 18.3.1, 18.3.1, 0.23.2 | MIT | Copyright (c) Facebook, Inc. and its affiliates. |
| react-is | 16.13.1 | MIT | Copyright (c) Facebook, Inc. and its affiliates. |
| prop-types | 15.8.1 | MIT | Copyright (c) 2013-present, Facebook, Inc. |
| react-router, react-router-dom | 7.18.4 | MIT | Copyright (c) React Training LLC 2015-2019 |
| cookie | 1.1.1 | MIT | Copyright (c) 2012-2014 Roman Shtylman |
| set-cookie-parser | 2.7.2 | MIT | Copyright (c) 2015 Nathan Friedly |
| react-plaid-link | 5.0.0 | MIT | Copyright (c) 2019 Plaid Technologies, Inc. |
| recharts | 2.15.4 | MIT | Copyright (c) 2015-present recharts |
| react-smooth | 4.0.4 | MIT | Copyright (c) 2016 recharts |
| recharts-scale | 0.4.5 | MIT | Copyright (c) 2015 Sen Yang |
| clsx | 2.1.1 | MIT | Copyright (c) Luke Edwards |
| eventemitter3 | 4.0.7 | MIT | Copyright (c) 2014 Arnout Kazemier |
| lodash | 4.18.1 | MIT | Copyright OpenJS Foundation and other contributors |
| fast-equals | 5.4.3 | MIT | Copyright (c) 2025 Tony Quetano |
| react-transition-group | 4.4.5 | BSD-3-Clause | Copyright (c) 2018, React Community |
| dom-helpers | 5.2.1 | MIT | Copyright (c) 2015 Jason Quense |
| @babel/runtime | 7.29.7 | MIT | Copyright (c) 2014-present Sebastian McKenzie and other contributors |
| object-assign | 4.1.1 | MIT | Copyright (c) Sindre Sorhus |
| decimal.js-light | 2.5.1 | MIT | Copyright (c) 2020 Michael Mclaughlin |
| tiny-invariant | 1.3.3 | MIT | Copyright (c) 2019 Alexander Reardon |
| victory-vendor | 36.9.2 | MIT AND ISC | Copyright (c) Formidable Labs (packages the d3 modules below) |
| d3-array, d3-color, d3-format, d3-interpolate, d3-path, d3-scale, d3-shape, d3-time, d3-time-format, d3-timer, internmap | 3.2.4, 3.1.0, 3.1.2, 3.0.1, 3.1.0, 4.0.2, 3.2.0, 3.1.0, 4.1.0, 3.0.1, 2.0.3 | ISC | Copyright 2010-2026 Mike Bostock |
| d3-ease | 3.0.1 | BSD-3-Clause | Copyright 2010-2021 Mike Bostock |

## Font

| Font | Version | License |
|---|---|---|
| IBM Plex Sans (via @fontsource/ibm-plex-sans) | 5.3.0 | SIL Open Font License 1.1 |

Copyright 2019 IBM Corp. All rights reserved. The full license is in
`versions\<v>\web\licenses\IBM-Plex-Sans-OFL.txt` in the install folder
(`frontend/public/licenses/IBM-Plex-Sans-OFL.txt` in the source).

## License texts

### MIT

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

### ISC

Permission to use, copy, modify, and/or distribute this software for any purpose with or
without fee is hereby granted, provided that the above copyright notice and this permission
notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS
SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE
AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT,
NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE
OF THIS SOFTWARE.

### BSD-3-Clause

Redistribution and use in source and binary forms, with or without modification, are permitted
provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of
   conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of
   conditions and the following disclaimer in the documentation and/or other materials provided
   with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may be used to
   endorse or promote products derived from this software without specific prior written
   permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR
IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

### Apache-2.0, PSF-2.0, zlib, MIT-0

- Apache License 2.0: <https://www.apache.org/licenses/LICENSE-2.0>
- Python Software Foundation License 2.0: <https://docs.python.org/3.11/license.html>
- zlib/libpng License: <https://opensource.org/license/zlib>
- MIT No Attribution (MIT-0): <https://opensource.org/license/mit-0>

Each package's own copy of its license is installed with it (see above).
