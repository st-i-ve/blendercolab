# Bundled fonts

The four selectable faces — **Heebo**, **Inter**, **Arimo** and
**Oswald** — are each licensed under the SIL Open Font License 1.1
(https://openfontlicense.org):

- Heebo — Copyright the Heebo Project Authors
  (https://github.com/OFL/Heebo)
- Inter — Copyright the Inter Project Authors
  (https://github.com/rsms/inter)
- Arimo — Copyright the Arimo Project Authors
  (https://github.com/googlefonts/arimo)
- Oswald — Copyright the Oswald Project Authors
  (https://github.com/googlefonts/OswaldFont)

Each ships twice: a variable TTF for Qt's font database, which does not
take woff2, and a woff2 latin subset for the web view, which is a tenth
of the size. Downloaded from Google Fonts and vendored here rather than
linked: a desktop app must not need the network to render its own text,
which is the same rule the Roboto files below are here for.

**Roboto** and **Roboto Mono** — Copyright Google LLC, licensed under the
Apache License 2.0. https://www.apache.org/licenses/LICENSE-2.0

Both are redistributable in binary form with attribution. Roboto Mono is the
same family as Roboto, so the app keeps a single typographic voice while
still setting numeric and machine data in a fixed-width face — figures that
do not reflow as they tick.
