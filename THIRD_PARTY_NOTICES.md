# Third-party notices

Parts of this plugin are reworded forks of the following MIT-licensed skills. Each license is reproduced in full.

## ponytail

Forked: the core ruleset (the `## Build` section of `skills/lean/SKILL.md`) and `skills/lean-review/SKILL.md`, from the skills `ponytail` and `ponytail-review`.

```
MIT License

Copyright (c) 2026 DietrichGebert

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

## i-have-adhd

Forked: the answer rules (the `## Answer` section of `skills/lean/SKILL.md`) and the always-on flag idea, from the skill `i-have-adhd`.

```
MIT License

Copyright (c) 2026 Ayoub Ghriss

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

## mattpocock-skills (grilling)

Forked: `skills/grill/SKILL.md`, from the skill `grilling`.

```
MIT License

Copyright (c) 2026 Matt Pocock

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

The document reader (`scripts/read.py`, `scripts/reader/`) follows the logic of these MIT-licensed projects. Each license is reproduced in full.

## dochan

Ported: the container, `hp:switch`, image map, charts, footnotes, fields and span grid in `scripts/reader/formats/hwpx.py`; chart and group-geometry logic in `scripts/reader/charts.py` and the OOXML modules (`scripts/reader/formats/ooxml*.py`); the DocInfo character-shape and style records and distribution-document sections (ViewText) in `scripts/reader/formats/hwp_records.py`; the hwp section walk, controls, fields, tables, drawing objects, BinData and track-change range kinds in `scripts/reader/formats/hwp.py`, and the OLE chart wrapper in `scripts/reader/formats/hwp_chart.py`; the doc FIB walk, piece table, sprm operand sizes, FKPs, styles and table definitions in `scripts/reader/formats/doc_fib.py`; doc stream checks, the table stream, story ranges, fields, notes and tables in `scripts/reader/formats/doc.py`, pictures in `scripts/reader/formats/doc_pictures.py` and embedded objects in `scripts/reader/formats/doc_objects.py`; the OfficeArt record walk and BLIPs in `scripts/reader/officeart.py`; the MTEF record walk in `scripts/reader/formats/mtef.py`; the Office RC4 headers and keys in `scripts/reader/containers/officecrypt.py`; the number-format General, text, section, number, scientific and fraction rules in `scripts/reader/numfmt.py`; the BIFF8 RK numbers, BOUNDSHEET, FORMAT, XF, DATEMODE and SST records in `scripts/reader/formats/biff.py`; the xls sheet walk, ROW and COLINFO hidden flags and cached formula values in `scripts/reader/formats/xls.py`; the XOR check and array, the clear-record list and the RC4 block decryption in `scripts/reader/formats/xls_crypt.py`; the sheet MSODRAWING join, pib placements and anchor order in `scripts/reader/formats/xls_drawing.py`; the chart substream walk, series references, SIIndex caches and chart kinds in `scripts/reader/formats/biff_chart.py`; the active-sheet chart or worksheet choice and the MS Graph chart for an embedded workbook in `scripts/reader/formats/xls_embedded.py`; the ppt persist chain, its budgets and cycle guard in `scripts/reader/formats/ppt_records.py`; the ppt slide list, notes mapping and text atoms in `scripts/reader/formats/ppt.py`, and the style runs in `scripts/reader/formats/ppt_text.py`, the persist-object and Pictures RC4 decryption in `scripts/reader/formats/ppt_crypt.py`, and the shape anchors, group transforms, table groups and reading order in `scripts/reader/formats/ppt_shapes.py`, and the ExOleObjStg handling of embedded objects in `scripts/reader/formats/ppt_objects.py`; run hyperlinks and legacy slide comments in `scripts/reader/formats/ooxml_slides.py`; the xlsx streaming walk, hyperlink ranges, merges and legacy comments in `scripts/reader/formats/ooxml_sheets.py`; the pdf parser of dochan 1.21.0 (commit 29bb52b) in `scripts/reader/pdfnative/*.py`; the pdf text Fragment and writing direction, the Korean spacing table (`scripts/reader/formats/pdf_spacing.json`), the line join, paragraph merge, heading sizes and running header detection in `scripts/reader/formats/pdf_layout.py` and the rule-segment path collector and the table grid assembly (merged and nested cells, caps) in `scripts/reader/formats/pdf_tables.py`.

```
MIT License

Copyright (c) 2025 dochan contributors

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

## kordoc

Ported: numbering, alt-text stripping, layout-table unwrapping and final-state track changes, in `scripts/reader/formats/hwpx.py`, the numbering now shared through `scripts/reader/formats/hancom.py`; outline and note numbering, `%clk` placeholders and the equation (EQEDIT) script in `scripts/reader/formats/hwp.py`; the NUMBERING, BULLET and PARA_SHAPE records in `scripts/reader/formats/hwp_records.py`.

```
MIT License

Copyright (c) 2026 chrisryugj

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

## anydoc

Ported: the style chain, span grid, row templates and display text in `scripts/reader/formats/odf*.py`; relationship discovery, style cascade, numbering, `Requires`-aware AlternateContent and grid in `scripts/reader/formats/ooxml*.py`; the CSS `display:none` cascade and span grid in `scripts/reader/formats/epub.py`, `scripts/reader/css.py` and `scripts/reader/html.py`; the Windows code page of a language id in `scripts/reader/text.py` and the Far East language choice and the fallback table stream in `scripts/reader/formats/doc.py`; the Prm0 sprm table, style walk, toggle properties, and the PlfLst and PlfLfo walk with its list counters in `scripts/reader/formats/doc_fib.py`; the hidden-row and hidden-column merge rule of the sheet grid in `scripts/reader/formats/sheet_grid.py`; the BIFF8 record walk and CONTINUE string rule in `scripts/reader/formats/biff.py`; the xls sheet walk, ROW and COLINFO hidden flags and cached formula values in `scripts/reader/formats/xls.py`; the BIFF12 record framing in `scripts/reader/containers/biff12.py`; the xlsb workbook, styles, shared strings, rows, cells and merges in `scripts/reader/formats/xlsb.py`; the ppt persist directory merge in `scripts/reader/formats/ppt_records.py`; the ppt slide list, hidden flag and notes mapping in `scripts/reader/formats/ppt.py`, the style runs in `scripts/reader/formats/ppt_text.py`, the RTF lexer and font charset table in `scripts/reader/formats/rtf_lex.py`, the RTF group state, destination list, special characters, notes, fields, headers, list text and pictures in `scripts/reader/formats/rtf.py`, and the RTF row and cell state and column grid in `scripts/reader/formats/rtf_table.py`; the drawing anchors in `scripts/reader/formats/sheet_drawing.py`; the OMML walk and the TeX buffer, symbol, delimiter, accent and function-name tables in `scripts/reader/formats/omml.py`; the xlsx shared strings and rich text without phonetics, styles and cell types in `scripts/reader/formats/ooxml_sheets.py`.

```
MIT License

Copyright (c) 2026 Sideguide Technologies Inc.

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

## docling

Ported: hidden sections and slides, comments, notes fields and the chart local table in `scripts/reader/formats/odf*.py`; `w:vanish`, hidden slides and outline level handling in `scripts/reader/formats/ooxml*.py`; spine merge and link rewrite in `scripts/reader/formats/epub.py`; the raw Snappy block and IWA chunk stream in `scripts/reader/containers/snappy.py` and the protobuf wire reader in `scripts/reader/containers/pbwire.py`, from `docling/backend/iwork/iwa.py`; the iWork container and the IWA object store in `scripts/reader/formats/iwork*.py`, from `docling/backend/iwork_backend.py`, `docling/backend/iwork/iwa.py`, `archives.py` and `legacy.py`; the text storages (styles, lists, links, notes, comments) and the Pages walk in `scripts/reader/formats/iwork_text.py` and `scripts/reader/formats/iwork_iwa.py`, from `docling/backend/iwork/content.py`, `archives.py` and `pages_iwa.py`; the table tiles, value lists and version 4 cells in `scripts/reader/formats/iwork_tables.py` and the Numbers walk in `scripts/reader/formats/iwork_iwa.py`, from `docling/backend/iwork/cells.py`, `archives.py` and `numbers_iwa.py`; the charts in `scripts/reader/formats/iwork_tables.py` and the Keynote walk in `scripts/reader/formats/iwork_iwa.py`, from `docling/backend/iwork/charts.py`, `keynote_iwa.py` and `keynote_content.py`; the iWork '09 XML reader (the `sf` vocabulary, the Pages, Keynote and Numbers walks and the '09 tables) in `scripts/reader/formats/iwork_xml.py`, from `docling/backend/iwork/legacy.py`, `pages_xml.py`, `keynote_xml.py` and `numbers_xml.py`; all at docling v2.150.0 (1ae470a1f4580881ed73853f64e0bf178f623c5e).

```
MIT License

Copyright The Docling Contributors

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

## xberg

Ported: group recursion, master pages and the hidden sheet marker in `scripts/reader/formats/odf*.py`; SDT placeholder drop in `scripts/reader/formats/ooxml*.py`; DRM skip, fallback chains and nav/TOC removal in `scripts/reader/formats/epub.py`; the BIFF12 record size framing in `scripts/reader/containers/biff12.py`; the RTF font encoding and code page choice in `scripts/reader/formats/rtf_lex.py`; the RTF hidden runs, `\uc` stack and fallback skip, and the picture format words and DIB picture handling, in `scripts/reader/formats/rtf.py`.

```
MIT License

Copyright (c) 2025-2026 Kreuzberg, Inc.

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

## rhwp

Ported: the per-item BinData compression bit, the control-character classes, the DRM sniff, the track-changes ViewText preference and the range-tag kinds in `scripts/reader/formats/hwp.py`; ViewText decoding in `scripts/reader/formats/hwp_records.py`; the Hancom-native OLE chart (probe, data grid, series orientation, title and chart type) in `scripts/reader/formats/hwp_chart.py`.

```
MIT License

Copyright (c) 2025-2026 Edward Kim

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

## tika

Ported: the PalmDB record table, trailing entries, PalmDOC LZ77 and HUFF/CDIC decoding in `scripts/reader/containers/palmdb.py`, from `tika-parser-miscoffice-module/.../parser/mobi/` (`PalmDatabase.java`, `PalmDoc.java`, `HuffCdic.java`) at Apache Tika main commit a40d54f6. The MOBI record 0 and EXTH head, the DRM, encryption, compression and encoding refusals and the EXTH caps in `scripts/reader/formats/mobi.py`, from `MobiHeader.java` and `MobiParser.java` at the same commit. The WordPerfect 5.x and 6+ extended character tables in `scripts/reader/formats/wpd_charsets.py`, from `tika-parser-miscoffice-module/.../parser/wordperfect/` (`WP6Charsets.java`, `WP5Charsets.java`) at Apache Tika tag 4.2.0 (a different pin from the mobi files above, which stay at main commit a40d54f6). The outline of the WordPerfect 6+ body walk in `scripts/reader/formats/wpd.py` (its byte classes and the undo state), from `WP6DocumentAreaExtractor.java` at main commit a40d54f6, and its 32 extended international characters, Tika's `DEFAULT_EXTENDED_INTL_CHARS` in `WP6Charsets.java` at tag 4.2.0.

Apache Tika NOTICE:

```
Apache Tika
Copyright 2007-2026 The Apache Software Foundation

This product includes software developed at
The Apache Software Foundation (http://www.apache.org/).

Copyright 1993-2010 University Corporation for Atmospheric Research/Unidata
This software contains code derived from UCAR/Unidata's NetCDF library.

tika-readability is a Java port of Mozilla's Readability.js (https://github.com/mozilla/readability),
licensed under the Apache License 2.0:
  Readability
  Copyright (c) 2010 Arc90 Inc
  Copyright (c) 2010-2026 Mozilla and Contributors

Tika-parsers component uses CDDL/LGPL dual-licensed dependency: jhighlight (https://github.com/codelibs/jhighlight)

IPTC Photo Metadata descriptions Copyright 2010 International Press Telecommunications Council.

Tika-mimetypes.xml includes mimetype definitions that were adapted from the PRONOM Technical Registry
by The National Archives (http://www.nationalarchives.gov.uk/PRONOM/Default.aspx). PRONOM is published
under the Open Government License 3.0 (http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
```

Apache License 2.0:

```
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.



APACHE TIKA SUBCOMPONENTS

Apache Tika includes a number of subcomponents with separate copyright notices
and license terms. Your use of these subcomponents is subject to the terms and
conditions of the following licenses.
-------------------------------
Big5 and EUC-JP decode patch tables in tika-core (org/apache/tika/detect/*-WHATWG.tsv),
derived from index-big5.txt and index-jis0208.txt of the WHATWG Encoding Standard
(https://encoding.spec.whatwg.org/)

    Copyright (c) WHATWG (Apple, Google, Mozilla, Microsoft).

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:
    1. Redistributions of source code must retain the above copyright notice,
       this list of conditions and the following disclaimer.
    2. Redistributions in binary form must reproduce the above copyright notice,
       this list of conditions and the following disclaimer in the documentation
       and/or other materials provided with the distribution.
    3. Neither the name of the copyright holder nor the names of its
       contributors may be used to endorse or promote products derived from
       this software without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
    AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
    IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
    ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
    LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
    CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.

-------------------------------
MIME type information from file-4.26.tar.gz (http://www.darwinsys.com/file/)

    Copyright (c) Ian F. Darwin 1986, 1987, 1989, 1990, 1991, 1992, 1994, 1995.
    Software written by Ian F. Darwin and others;
    maintained 1994- Christos Zoulas.

    This software is not subject to any export provision of the United States
    Department of Commerce, and may be exported to any country or planet.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions
    are met:
    1. Redistributions of source code must retain the above copyright
       notice immediately at the beginning of the file, without modification,
       this list of conditions, and the following disclaimer.
    2. Redistributions in binary form must reproduce the above copyright
       notice, this list of conditions and the following disclaimer in the
       documentation and/or other materials provided with the distribution.

    THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
    ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
    IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
    ARE DISCLAIMED. IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE FOR
    ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
    DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
    OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
    HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
    LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
    OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
    SUCH DAMAGE.

-------------------------------
Charset detection code from ICU4J (http://site.icu-project.org/)

    Copyright (c) 1995-2009 International Business Machines Corporation
    and others

    All rights reserved.

    Permission is hereby granted, free of charge, to any person obtaining
    a copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, and/or sell copies of the Software, and to permit persons
    to whom the Software is furnished to do so, provided that the above
    copyright notice(s) and this permission notice appear in all copies
    of the Software and that both the above copyright notice(s) and this
    permission notice appear in supporting documentation.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
    OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF THIRD PARTY RIGHTS.
    IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
    BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
    OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
    WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
    ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS
    SOFTWARE.

    Except as contained in this notice, the name of a copyright holder shall
    not be used in advertising or otherwise to promote the sale, use or other
    dealings in this Software without prior written authorization of the
    copyright holder.

-------------------------------
Parsing functionality provided by the NetCDF Java Library (http://www.unidata.ucar.edu/software/netcdf-java/)
    
    Copyright 1993-2010 University Corporation for Atmospheric Research/Unidata
    
    Portions of this software were developed by the Unidata Program at the University 
    Corporation for Atmospheric Research.
    
    Access and use of this software shall impose the following obligations and understandings 
    on the user. The user is granted the right, without any fee or cost, to use, copy, modify, 
    alter, enhance and distribute this software, and any derivative works thereof, and its 
    supporting documentation for any purpose whatsoever, provided that this entire notice 
    appears in all copies of the software, derivative works and supporting documentation. Further, 
    UCAR requests that the user credit UCAR/Unidata in any publications that result from the use 
    of this software or in any product that includes this software, although this is not an obligation. 
    The names UCAR and/or Unidata, however, may not be used in any advertising or publicity to endorse 
    or promote any products or commercial entity unless specific written permission is obtained from 
    UCAR/Unidata. The user also understands that UCAR/Unidata is not obligated to provide the user with 
    any support, consulting, training or assistance of any kind with regard to the use, operation and 
    performance of this software nor to provide the user with any updates, revisions, new versions or 
    "bug fixes."
    
    THIS SOFTWARE IS PROVIDED BY UCAR/UNIDATA "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, 
    BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE 
    ARE DISCLAIMED. IN NO EVENT SHALL UCAR/UNIDATA BE LIABLE FOR ANY SPECIAL, INDIRECT OR CONSEQUENTIAL 
    DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION 
    OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE ACCESS, 
    USE OR PERFORMANCE OF THIS SOFTWARE.

-------------------------------
IPTC Photo Metadata descriptions are taken from the IPTC Photo Metadata 
Standard, July 2010, Copyright 2010 International Press Telecommunications 
Council.

  1. The Specifications and Materials are licensed for use only on the condition that you agree to be bound by the terms of this license. Subject to this and other licensing requirements contained herein, you may, on a non-exclusive basis, use the Specifications and Materials.
  2. The IPTC openly provides the Specifications and Materials for voluntary use by individuals, partnerships, companies, corporations, organizations and any other entity for use at the entity's own risk. This disclaimer, license and release is intended to apply to the IPTC, its officers, directors, agents, representatives, members, contributors, affiliates, contractors, or co-venturers acting jointly or severally.
  3. The Document and translations thereof may be copied and furnished to others, and derivative works that comment on or otherwise explain it or assist in its implementation may be prepared, copied, published and distributed, in whole or in part, without restriction of any kind, provided that the copyright and license notices and references to the IPTC appearing in the Document and the terms of this Specifications License Agreement are included on all such copies and derivative works. Further, upon the receipt of written permission from the IPTC, the Document may be modified for the purpose of developing applications that use IPTC Specifications or as required to translate the Document into languages other than English.
  4. Any use, duplication, distribution, or exploitation of the Document and Specifications and Materials in any manner is at your own risk.
  5. NO WARRANTY, EXPRESSED OR IMPLIED, IS MADE REGARDING THE ACCURACY, ADEQUACY, COMPLETENESS, LEGALITY, RELIABILITY OR USEFULNESS OF ANY INFORMATION CONTAINED IN THE DOCUMENT OR IN ANY SPECIFICATION OR OTHER PRODUCT OR SERVICE PRODUCED OR SPONSORED BY THE IPTC. THE DOCUMENT AND THE INFORMATION CONTAINED HEREIN AND INCLUDED IN ANY SPECIFICATION OR OTHER PRODUCT OR SERVICE OF THE IPTC IS PROVIDED ON AN "AS IS" BASIS. THE IPTC DISCLAIMS ALL WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, ANY ACTUAL OR ASSERTED WARRANTY OF NON-INFRINGEMENT OF PROPRIETARY RIGHTS, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE. NEITHER THE IPTC NOR ITS CONTRIBUTORS SHALL BE HELD LIABLE FOR ANY IMPROPER OR INCORRECT USE OF INFORMATION. NEITHER THE IPTC NOR ITS CONTRIBUTORS ASSUME ANY RESPONSIBILITY FOR ANYONE'S USE OF INFORMATION PROVIDED BY THE IPTC. IN NO EVENT SHALL THE IPTC OR ITS CONTRIBUTORS BE LIABLE TO ANYONE FOR DAMAGES OF ANY KIND, INCLUDING BUT NOT LIMITED TO, COMPENSATORY DAMAGES, LOST PROFITS, LOST DATA OR ANY FORM OF SPECIAL, INCIDENTAL, INDIRECT, CONSEQUENTIAL OR PUNITIVE DAMAGES OF ANY KIND WHETHER BASED ON BREACH OF CONTRACT OR WARRANTY, TORT, PRODUCT LIABILITY OR OTHERWISE.
  6. The IPTC takes no position regarding the validity or scope of any Intellectual Property or other rights that might be claimed to pertain to the implementation or use of the technology described in the Document or the extent to which any license under such rights might or might not be available. The IPTC does not represent that it has made any effort to identify any such rights. Copies of claims of rights made available for publication, assurances of licenses to be made available, or the result of an attempt made to obtain a general license or permission for the use of such proprietary rights by implementers or users of the Specifications and Materials, can be obtained from the Managing Director of the IPTC.
  7. By using the Specifications and Materials including the Document in any manner or for any purpose, you release the IPTC from all liabilities, claims, causes of action, allegations, losses, injuries, damages, or detriments of any nature arising from or relating to the use of the Specifications, Materials or any portion thereof. You further agree not to file a lawsuit, make a claim, or take any other formal or informal legal action against the IPTC, resulting from your acquisition, use, duplication, distribution, or exploitation of the Specifications, Materials or any portion thereof. Finally, you hereby agree that the IPTC is not liable for any direct, indirect, special or consequential damages arising from or relating to your acquisition, use, duplication, distribution, or exploitation of the Specifications, Materials or any portion thereof.
  8. Specifications and Materials may be downloaded or copied provided that ALL copies retain the ownership, copyright and license notices.
  9. Materials may not be edited, modified, or presented in a context that creates a misleading or false impression or statement as to the positions, actions, or statements of the IPTC.
  10. The name and trademarks of the IPTC may not be used in advertising, publicity, or in relation to products or services and their names without the specific, written prior permission of the IPTC. Any permitted use of the trademarks of the IPTC, whether registered or not, shall be accompanied by an appropriate mark and attribution, as agreed with the IPTC.
  11. Specifications may be extended by both members and non-members to provide additional functionality (Extension Specifications) provided that there is a clear recognition of the IPTC IP and its ownership in the Extension Specifications and the related documentation and provided that the extensions are clearly identified and provided that a perpetual license is granted by the creator of the Extension Specifications for other members and non-members to use the Extension Specifications and to continue extensions of the Extension Specifications. The IPTC does not waive any of its rights in the Specifications and Materials in this context. The Extension Specifications may be considered the intellectual property of their creator. The IPTC expressly disclaims any responsibility for damage caused by an extension to the Specifications.
  12. Specifications and Materials may be included in derivative work of both members and non-members provided that there is a clear recognition of the IPTC IP and its ownership in the derivative work and its related documentation. The IPTC does not waive any of its rights in the Specifications and Materials in this context. Derivative work in its entirety may be considered the intellectual property of the creator of the work .The IPTC expressly disclaims any responsibility for damage caused when its IP is used in a derivative context.
  13. This Specifications License Agreement is perpetual subject to your conformance to the terms of this Agreement. The IPTC may terminate this Specifications License Agreement immediately upon your breach of this Agreement and, upon such termination you will cease all use, duplication, distribution, and/or exploitation in any manner of the Specifications and Materials.
  14. This Specifications License Agreement reflects the entire agreement of the parties regarding the subject matter hereof and supersedes all prior agreements or representations regarding such matters, whether written or oral. To the extent any portion or provision of this Specifications License Agreement is found to be illegal or unenforceable, then the remaining provisions of this Specifications License Agreement will remain in full force and effect and the illegal or unenforceable provision will be construed to give it such effect as it may properly have that is consistent with the intentions of the parties.
  15. This Specifications License Agreement may only be modified in writing signed by an authorized representative of the IPTC.
  16. This Specifications License Agreement is governed by the law of United Kingdom, as such law is applied to contracts made and fully performed in the United Kingdom. Any disputes arising from or relating to this Specifications License Agreement will be resolved in the courts of the United Kingdom. You consent to the jurisdiction of such courts over you and covenant not to assert before such courts any objection to proceeding in such forums.

-------------------------------
JUnRAR (https://github.com/edmund-wagner/junrar/)

      JUnRAR is based on the UnRAR tool, and covered by the same license
      It was formerly available from http://java-unrar.svn.sourceforge.net/

 ******    *****   ******   UnRAR - free utility for RAR archives
 **   **  **   **  **   **  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 ******   *******  ******    License for use and distribution of
 **   **  **   **  **   **   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 **   **  **   **  **   **         FREE portable version
                                   ~~~~~~~~~~~~~~~~~~~~~

      The source code of UnRAR utility is freeware. This means:

   1. All copyrights to RAR and the utility UnRAR are exclusively
      owned by the author - Alexander Roshal.

   2. The UnRAR sources may be used in any software to handle RAR
      archives without limitations free of charge, but cannot be used
      to re-create the RAR compression algorithm, which is proprietary.
      Distribution of modified UnRAR sources in separate form or as a
      part of other software is permitted, provided that it is clearly
      stated in the documentation and source comments that the code may
      not be used to develop a RAR (WinRAR) compatible archiver.

   3. The UnRAR utility may be freely distributed. It is allowed
      to distribute UnRAR inside of other software packages.

   4. THE RAR ARCHIVER AND THE UnRAR UTILITY ARE DISTRIBUTED "AS IS".
      NO WARRANTY OF ANY KIND IS EXPRESSED OR IMPLIED.  YOU USE AT 
      YOUR OWN RISK. THE AUTHOR WILL NOT BE LIABLE FOR DATA LOSS, 
      DAMAGES, LOSS OF PROFITS OR ANY OTHER KIND OF LOSS WHILE USING
      OR MISUSING THIS SOFTWARE.

   5. Installing and using the UnRAR utility signifies acceptance of
      these terms and conditions of the license.

   6. If you don't agree with terms of the license you must remove
      UnRAR files from your storage devices and cease to use the
      utility.

      Thank you for your interest in RAR and UnRAR.  Alexander L. Roshal

-------------------------------
Sqlite (optional) (bundled in org.xerial's sqlite-jdbc)
    This product bundles Sqlite, which is in the Public Domain.  For details
    see: https://www.sqlite.org/copyright.html

-------------------------------
Sample DXF file testDXF.dxf (in tika-parsers/src/test/resources/test-documents)
     Copyright 2012 Ho Thanh Tam, www.cadkit.net

     Permission to use, copy, modify, and distribute this software and its 
     documentation for any purpose is hereby granted without fee, provided 
     that the above copyright notice, author statement appear in all copies 
     of this software and related documentation.

-------------------------------
H2 Database in tika-eval
    This software contains unmodified binary redistributions for
    H2 database engine (http://www.h2database.com/),
    which is dual licensed and available under the MPL 2.0
    (Mozilla Public License) or under the EPL 1.0 (Eclipse Public License).
    An original copy of the license agreement can be found at:
    http://www.h2database.com/html/license.html

-------------------------------
org.brotli.dec dependency of commons-compress (MIT License)

Copyright (c) 2009, 2010, 2013-2016 by the Brotli Authors.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

----------------------------------------------
quine.gz test file (MIT License)
https://twitter.com/WhoStoleHonno/status/1153315367235784704?s=20

Copyright (c) 2019 by Matthew Barber.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

com.googlecode.plist:dd-plist
dd-plist - An open source library to parse and generate property lists
Copyright (C) 2016 Daniel Dreibrodt

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## foliate-js

Ported: the KF8 FDST flows, the INDX, TAGX and CNCX index reader, the SKEL and FRAG tables and their assembly, and the `kindle:flow`, `kindle:embed` and `kindle:pos` links in `scripts/reader/formats/mobi.py`, from `mobi.js` at foliate-js commit 78914aef4466eb960965702401634c2cb348e9b1.

```
MIT License

Copyright (c) 2022 John Factotum

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

## numbers-parser

Ported: `scripts/reader/formats/iwork_tables.py` (cell v5 order, decimal128, formats), from `numbers_parser/cell.py`, `numbers_parser/model.py` and `numbers_parser/constants.py` (the five currency symbols from `numbers_parser/currencies.py`) at numbers-parser v4.22.2 (commit f783064231a796df8ab5ef66daf7078c705df508). Apple's protobuf schemas in its `protos/` were read as format knowledge only and are not distributed.

```
Copyright 2021 Jon Connell

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE
```

## keynote-parser

Ported: `scripts/reader/formats/iwork_iwa.py` (the Keynote field numbers of `KN.DocumentArchive`, `ShowArchive`, `SlideTreeArchive`, `SlideNodeArchive`, `SlideArchive`, `NoteArchive` and `PlaceholderArchive`, taken as facts), from `protos/versions/14.5/KNArchives.proto` at keynote-parser tag v1.14.5.0 (commit 9f201c0eaa3d3e80a88e00995ed17ce999888cc8), read on the web. Its protobuf schemas are Apple's; they were read as format knowledge only and are not distributed. The licence text is the copy numbers-parser ships (`licenses/keynote-parser.txt`).

```
Copyright 2019-2025 Peter Sobot

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## djvu-rs

Ported: the BZZ decoder (the ZP coder, its adaptation table, the MTF stage and the inverse BWT) in `scripts/reader/containers/bzz.py`, from `djvu-bzz` 0.45.0 (`src/decode.rs`, `src/lib.rs`) and `djvu-zp` 0.45.0 (`src/lib.rs`, `src/tables.rs`); the BZZ and ZP encoder of the test fixtures in `tests/djvu_fix.py`, from `djvu-bzz` 0.45.0 (`src/encode.rs`) and `djvu-zp` 0.45.0 (`src/encoder.rs`); both crates at commit a329e2ad3bd9f710fcfeb758939f58626a6eb69a. The ZP table is djvu-zp's `src/tables.rs`, rows 0-250 (its rows 251-255 are padding and are left out); spot values were compared with DjVuLibre's `default_ztable` as format knowledge.

```
MIT License

Copyright (c) 2026 Lev Matyushkin

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

## jackcess

Ported: the Jet and ACE page layer (page 0 and its header mask, the version codes and page sizes, the code page and encoding key, the db_key page codec, inline and reference usage maps, page chains and the compressed unicode text form) in `scripts/reader/formats/jet_pages.py`, from `com/healthmarketscience/jackcess/impl/` (`JetFormat.java`, `PageChannel.java`, `UsageMap.java`, `CodecHandler.java`, `DefaultCodecProvider.java`, `DatabaseImpl.java` and `ColumnImpl.java`), and the Access reader (the database password and its creation-date mask, the catalog, table definitions with their column and index blocks, rows with their null mask, Jet3 jump table, deleted and overflow bits, long values and the scalar value decoders, the LvProp property blob with its name list and column maps (Format, TextFormat, Required, Expression, ResultType), the relationships of MSysRelationships, the hidden, hyperlink and autonumber column flags, the primary key the rows are ordered by, complex columns (MSysComplexColumns, the type tables' names, the flat tables, attachments and their wrapper, multi-value and version history columns) and calculated columns (the wrapped value and the Decimal form) in `scripts/reader/formats/jet.py`, from the same folder (`DatabaseImpl.java`, `TableImpl.java`, `ColumnImpl.java`, `MemoColumnImpl.java`, `NumericColumnImpl.java`, `IndexData.java`, `IndexImpl.java`, `LongValueColumnImpl.java`, `PropertyMaps.java`, `UsageMap.java`, `NullMask.java`, `JetFormat.java`, `ComplexColumnSupport.java`, `CalculatedColumnUtil.java` and `complex/*`: `ComplexColumnInfoImpl.java`, `AttachmentColumnInfoImpl.java`, `MultiValueColumnInfoImpl.java` and `VersionHistoryColumnInfoImpl.java`), of Jackcess 5.0.4, read from its Maven Central sources jar (`jackcess-5.0.4-sources.jar`, SHA-1 bba7e080c838c4b77704e509efc9cf336d578d6a); the Jet writer of the test fixtures in `tests/jet_fix.py` follows the layouts of the same release (`TableImpl.java`, `ColumnImpl.java`, `IndexData.java`, `IndexImpl.java`, `LongValueColumnImpl.java`, `PropertyMaps.java`, `ComplexColumnSupport.java`, `complex/ComplexColumnInfoImpl.java`, `complex/AttachmentColumnInfoImpl.java`, `CalculatedColumnUtil.java`); the saved queries rebuilt as SQL text (never run) in `scripts/reader/formats/jet_sql.py`, from `impl/query/` of the same jar (`QueryImpl.java`, `QueryFormat.java`, `BaseSelectQueryImpl.java`, `SelectQueryImpl.java`, `MakeTableQueryImpl.java`, `AppendQueryImpl.java`, `UpdateQueryImpl.java`, `DeleteQueryImpl.java`, `CrossTabQueryImpl.java`, `DataDefinitionQueryImpl.java`, `PassthroughQueryImpl.java` and `UnionQueryImpl.java`), whose test cases in `tests/test_reader_jet_sql.py` are ported from `src/test/java/com/healthmarketscience/jackcess/query/QueryTest.java` of the Jackcess repository (mirror commit be0a2139b087a227d14cafd4ea231306a5a3a862), the expected SQL following the jar where the two differ. mdbtools' `HACKING.md` (LGPL) was read as format knowledge only. Jackcess is licensed under the Apache License 2.0, whose text is reproduced under tika above. The sources jar carries no NOTICE file; the Jackcess repository's `NOTICE.txt` reads:

```
Many of the financial functions in the expression engine have been originally
copied from the Apache POI project (Apache Software Foundation) and the
UCanAccess Project.  They have been then modified and adapted so that they are
integrated with Jackcess, in a consistent manner.  The Apache POI and
UCanAccess projects are both licensed under Apache License, Version 2.0
http://www.apache.org/licenses/LICENSE-2.0.
```

Copyright notices of the source files named above:

```
Copyright (c) 2005 Health Market Science, Inc.
Copyright (c) 2007 Health Market Science, Inc.
Copyright (c) 2008 Health Market Science, Inc.
Copyright (c) 2010 James Ahlborn
Copyright (c) 2011 James Ahlborn
Copyright (c) 2013 James Ahlborn
Copyright (c) 2014 James Ahlborn
```

## poi

Ported: the pointer cap of the Visio pointer tree (100,000 pointers a document, POI's `MAX_NUMBER_OF_POINTERS`) in `scripts/reader/formats/vsd_stream.py`, from `poi-scratchpad/src/main/java/org/apache/poi/hdgf/pointers/PointerFactory.java`; the Quill bit layout (the `CHNKINK ` head, the bit directory entry fields, the PLC bits' count and type, and the type 12 hyperlink list with its 1,000-PLC cap) in `scripts/reader/formats/pub_quill.py`, from `poi-scratchpad/src/main/java/org/apache/poi/hpbf/model/QuillContents.java` and `poi-scratchpad/src/main/java/org/apache/poi/hpbf/model/qcbits/QCPLCBit.java`; both of Apache POI at trunk commit 47dcff769bb395034dddfd59c4609659b6dbc6a4. Apache POI is licensed under the Apache License 2.0, whose text is reproduced under tika above. Its NOTICE (`legal/NOTICE` at the same commit) reads:

```
Apache POI
Copyright 2003-2026 The Apache Software Foundation

This product includes software developed at
The Apache Software Foundation (https://www.apache.org/).

This product contains parts that were originally based on software from BEA.
Copyright (c) 2000-2003, BEA Systems, <http://www.bea.com/> (dead link),
which was acquired by Oracle Corporation in 2008.
<http://www.oracle.com/us/corporate/Acquisitions/bea/index.html>
<https://en.wikipedia.org/wiki/BEA_Systems>
Note: The ASF Secretary has on hand a Software Grant Agreement (SGA) from
BEA Systems, Inc. dated 9 Sep 2003 for XMLBeans signed by their EVP/CFO.

This product contains W3C XML Schema documents. Copyright 2001-2003 (c)
World Wide Web Consortium (Massachusetts Institute of Technology, European
Research Consortium for Informatics and Mathematics, Keio University)

This product contains the chunks_parse_cmds.tbl file from the vsdump program.
Copyright (C) 2006-2007 Valek Filippov (frob@df.ru)

This product contains parts of the eID Applet project
<http://eid-applet.googlecode.com> and <https://github.com/e-Contract/eid-applet>.
Copyright (c) 2009-2018
FedICT (federal ICT department of Belgium), e-Contract.be BVBA (https://www.e-contract.be),
Bart Hanssens from FedICT

ExceptionUtils is derived from `scala.util.control.NonFatal` in scala-library
which was released under the Apache 2.0 license.

Copyright (c) 2002-2023 EPFL
Copyright (c) 2011-2023 Lightbend, Inc.

Scala includes software developed at
LAMP/EPFL (https://lamp.epfl.ch/) and
Lightbend, Inc. (https://www.lightbend.com/).
```

## libvisio

Ported: the VisioDocument header, the LZ77 inflate, the Visio 5, 2000 and 2002-2013 pointers, pointer lists and chunk headers with their trailer rules, and the walk of the pointer tree from the trailer in `scripts/reader/formats/vsd_stream.py`, from `src/lib/VSDInternalStream.cpp`, `src/lib/VSDParser.cpp`, `src/lib/VSD6Parser.cpp` and `src/lib/VSD5Parser.cpp` of libvisio at commit a42aaff13e4a67afcd077c3abb3ef13bb6463025; the Visio 5, 2000 and 2002-2013 page, stencil and shape context by stream levels, the Visio 5 list chunk records, the page name lists and name indexes, the shape's XForm, text, names, field list and text fields (the number and date formats and a field mark taking the next value), its master (stencil) shape and the stencil shape lookup, its misc chunk (the hidden-text flag and the glued connector ends), the font charsets and character runs of 8-bit text, and the foreign data type and bytes in `scripts/reader/formats/vsd.py`, from `src/lib/VSDParser.cpp`, `src/lib/VSD6Parser.cpp`, `src/lib/VSD5Parser.cpp`, `src/lib/VSDStencils.cpp`, `src/lib/VSDFieldList.cpp` and `src/lib/VSDContentCollector.cpp` at the same commit. libvisio's later commits (2026-11-15) were read as knowledge only.

The files on the Ported: line above are licensed under the Mozilla Public License, v. 2.0 (MPL-2.0), https://mozilla.org/MPL/2.0/, and each starts with the MPL's notice and the commit and files it was ported from. The MPL covers those files only: the rest of this plugin stays under its own licence. libvisio's source files carry no copyright line beyond "This file is part of the libvisio project."

## libmspub

Ported: the Contents blocks (the data length of each block type, variable blocks, the reading of a block), the trailer and its chunk directory (the trailer directory, the chunk references with their type, offset and parent sequence number, and the content chunk types) and the Publisher 2000 directory table in `scripts/reader/formats/pub_contents.py`, from `src/lib/MSPUBParser.cpp`, `src/lib/MSPUBBlockType.h`, `src/lib/MSPUBBlockID.h`, `src/lib/MSPUBContentChunkType.h` and `src/lib/MSPUBParser2k.cpp`; the Quill bit directory and its linked lists, the stories (TEXT cut by the STRS lengths and keyed by the SYID text ids), the FDPC character runs and the bold flag of their styles, the FDPP paragraph runs and the default character styles of the second STSH bit that the bold is taken against, and the table cell ends of the `TCD ` bits in `scripts/reader/formats/pub_quill.py`, from `src/lib/MSPUBParser.cpp` and `src/lib/MSPUBCollector.cpp`; the Publisher 2002 and later reader (the document's page list, the page chunks with their shape lists, master names and the DUMMY page seqnums of the scratch area, the shape chunks' text ids, the pages written in order with their masters, the Escher shape groups, anchors, child anchors and client data that give each shape its box and its SpContainer, with the container tails and value headers of Publisher's Escher stream, the table chunks' rows, columns and cells chunk with each cell's start and end row and column, the table layout and the cell texts mapped to the cells, and each shape's picture by its BLIP index and the delay stream) in `scripts/reader/formats/pub.py`, from `src/lib/MSPUBParser.cpp`, `src/lib/MSPUBCollector.cpp`, `src/lib/EscherContainerType.h`, `src/lib/EscherFieldIds.h` and `src/lib/TableInfo.h`; the Publisher 2000 and 97/98 readers (the chunk types by their first u16, the pages' shapes by parent, the group members, the master and DUMMY page seqnums, the shape's box, the text box's type and text id offset, the picture's data chunk and its bytes, and the 97 text header, its text range and the paragraph, line and text box ends of its 8-bit text) in `scripts/reader/formats/pub_2k.py`, from `src/lib/MSPUBParser2k.cpp` and `src/lib/MSPUBParser97.cpp`; all of libmspub at commit 0477b85184e02dc5276582d9cea969208d56c655.

The files on the Ported: line above are licensed under the Mozilla Public License, v. 2.0 (MPL-2.0), https://mozilla.org/MPL/2.0/, and each starts with the MPL's notice and the commit and files it was ported from. The MPL covers those files only: the rest of this plugin stays under its own licence. libmspub's source files carry no copyright line beyond "This file is part of the libmspub project."

## Adobe

Data: `scripts/reader/pdfnative/cid_unicode_data.py` (the Adobe CID to Unicode tables of mapping-resources-pdf), `scripts/reader/pdfnative/predefined_cmap_data.py` (the predefined Encoding CMap ranges of cmap-resources) and `scripts/reader/pdfnative/core14_metrics.py` (the Core 14 AFM metrics and the Adobe Glyph List). The three modules are copied byte for byte from dochan 1.21.0 (commit 29bb52be2cf40750e0dab25b24d87e33d0fcedd7, `src/dochan/pdf/`), each with one added docstring line; dochan's NOTICE reproduces Adobe's notices as follows. The original Adobe CMap and AFM files are not distributed.

```
Adobe mapping-resources-pdf/pdf2unicode CID to Unicode data
Source: https://github.com/adobe-type-tools/mapping-resources-pdf/tree/master/pdf2unicode
Modified: scripts/generate_pdf_cid2unicode.py extracts mappings, excludes
U+FFFD replacement characters, and compresses the CID tables into
dochan/pdf/cid_unicode_data.py. Runtime filtering excludes control,
surrogate, private-use, and noncharacter mappings and strips variation
selectors, keeping the base character.
The original CMap files are not distributed with dochan.
SHA-256 Adobe-CNS1-UCS2: 8375afd535e153a7e3bcf448be93e5012b26229547a20d52520e46e3c386351f
SHA-256 Adobe-GB1-UCS2: 368b40ec05568faf323cfbbddf23c60d566bbc88e366981eb6541b026381a3b8
SHA-256 Adobe-Japan1-UCS2: 6a9693361647a37996312cc57071bb79f8c06411207be7c730a83fda1254cd82
SHA-256 Adobe-KR-UCS2: 6861a3208f331ecd73369c5d51eacf3b72c820fe3c87524f79c9ac1bb16830ec
SHA-256 Adobe-Korea1-UCS2: 45bcf869acdcec2507f75836919e589e7137b9f9dbaa8e4975747035024248ef
SHA-256 LICENSE.txt: feb8b068a417681821823ef2e8995072aa111121fa11f8664038f78717f4497c

Copyright lines as they appear in the source files:
  Adobe-CNS1-UCS2:   Copyright 1990-2020 Adobe. All rights reserved.
  Adobe-GB1-UCS2:    Copyright 1990-2020 Adobe. All rights reserved.
  Adobe-Japan1-UCS2: Copyright 1990-2023 Adobe. All rights reserved.
  Adobe-KR-UCS2:     Copyright 1990-2020 Adobe. All rights reserved.
  Adobe-Korea1-UCS2: Copyright 1990-2019 Adobe. All rights reserved.
  LICENSE.txt:       Copyright 1990-2019 Adobe. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

Redistributions of source code must retain the above copyright notice, this list
of conditions and the following disclaimer.

Redistributions in binary form must reproduce the above copyright notice, this
list of conditions and the following disclaimer in the documentation and/or
other materials provided with the distribution.

Neither the name of Adobe nor the names of its contributors may be used to
endorse or promote products derived from this software without specific prior
written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

Adobe Core 14 AFM metrics and Adobe Glyph List

Copyright (c) 1985, 1987, 1988, 1989, 1997 Adobe Systems Incorporated. All Rights Reserved.
Copyright (c) 1985, 1987, 1988, 1989, 1997 Adobe Systems Incorporated. All Rights Reserved.ITC Zapf Dingbats is a registered trademark of International Typeface Corporation.
Copyright (c) 1985, 1987, 1989, 1990, 1993, 1997 Adobe Systems Incorporated.  All Rights Reserved.
Copyright (c) 1985, 1987, 1989, 1990, 1993, 1997 Adobe Systems Incorporated.  All Rights Reserved.Times is a trademark of Linotype-Hell AG and/or its subsidiaries.
Copyright (c) 1985, 1987, 1989, 1990, 1997 Adobe Systems Incorporated.  All Rights Reserved.
Copyright (c) 1985, 1987, 1989, 1990, 1997 Adobe Systems Incorporated.  All Rights Reserved.Helvetica is a trademark of Linotype-Hell AG and/or its subsidiaries.
Copyright (c) 1985, 1987, 1989, 1990, 1997 Adobe Systems Incorporated. All rights reserved.
Copyright (c) 1989, 1990, 1991, 1992, 1993, 1997 Adobe Systems Incorporated.  All Rights Reserved.
Copyright (c) 1989, 1990, 1991, 1993, 1997 Adobe Systems Incorporated.  All Rights Reserved.

This file and the 14 PostScript(R) AFM files it accompanies may be used, copied, and distributed for any purpose and without charge, with or without modification, provided that all copyright notices are retained; that the AFM files are not distributed without this file; that all modifications to this file or any of the AFM files are prominently noted in the modified file(s); and that this paragraph is not modified. Adobe Systems has no responsibility or obligation to support the use of the AFM files.

Modified: dochan extracts only glyph names, advance widths (WX) and built-in encoding codes from the AFM files, adds a Unicode mapping using the Adobe Glyph List, and rebuilds the WinAnsi and MacRoman tables from Python codecs. The original AFM files are not distributed with dochan.
(AFM 에서 글리프 이름·폭·내장 인코딩 코드만 추출하고 Adobe Glyph List 로 유니코드 대응표를 더한 수정본이며, 원본 AFM 은 배포하지 않습니다.)

Adobe Glyph List and ITC Zapf Dingbats Glyph List (BSD-3-Clause):

Copyright 2002-2019 Adobe (http://www.adobe.com/).

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.

Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

Neither the name of Adobe nor the names of its contributors may be
used to endorse or promote products derived from this software without
specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

Adobe cmap-resources predefined PDF Encoding CMap factual ranges
Source: https://github.com/adobe-type-tools/cmap-resources
Modified: scripts/generate_pdf_predefined_cmaps.py parses and compresses
codespace, CID and notdef ranges; parent CMaps remain references.
Original CMap files are not distributed with dochan.
SHA-256 Adobe-CNS1-7/B5pc-H: 7a2af623d66158fd9b2eae590d3b0373b2e73125f008befda26c40a6c5d396fb; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/B5pc-V: f1b09f3752437a802f0992297a18361fd3bceed8e208cfbb97f2e6c035a29d2e; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/CNS-EUC-H: 61a5163ddf2dcbeed5efc4c8e9a612c7b3c5a0c062df75b2b3d73e3e46e7a299; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/CNS-EUC-V: 91faeda86c64df62c57b434a5b1322e1a6236c8520db3d4ee58b6ae5fee4c01e; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/ETen-B5-H: b6e5e42d94f6642842f6a42445c1df29d3874c6d1a91a38a05c9d7e4b57f0ce6; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/ETen-B5-V: 0e6f041edd60516e92c9470254c4d45522b4cf079f1809b82862d43a3f1e1663; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/ETenms-B5-H: 051a38392481d6069608f41962ef9393e142797a8977d7f7fbd534b051a6e231; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/ETenms-B5-V: 477e42501dd0c27301b992ab004d6044bf5406277ee7a353d3b04cc08092a9e0; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/HKscs-B5-H: 46b4d44a0d38cf188a88f42688f10a03aed8d67be80c141a1404abacc01ee191; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/HKscs-B5-V: 1f6f7f06216987d973c18e23d1a8c79044d9f1b301e01c738867ca1fdf50e237; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/UniCNS-UCS2-H: fe9c7f1557d58875bc4a16edf377ee0d8c579eaee778135adefbbed1f868c017; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/UniCNS-UCS2-V: 745fd7a922bedd2d124006a4b5e6a909aaaa9f11eebefbb0477c22a01439a3a8; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/UniCNS-UTF16-H: 074d175c8c6827184d064f2c48cbe0ba4957bd525fd864184717518acf4db970; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-CNS1-7/UniCNS-UTF16-V: d21175d1f8a083b606a9a1e17fa98d1cb901a0e833e19d47a6c08fb452c91c11; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GB-EUC-H: 9c2b268c5fbcc9a23aa76d2f199c97fa9fbfcc0ef43f9f320bb367a6b0a47b20; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GB-EUC-V: 5acf07ba8e65be8aad1c7a4535574e41cb326abd295e9e5c1eb3e96037260397; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GBK-EUC-H: 6c88b308f122966de14696d819800bc89be2538fa14fa41f474e36b20cea11c6; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GBK-EUC-V: 73381a5bc2d0aacbf6b2ec211f34e042e7c74895342fcee073b0f9be2dbc8703; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GBK2K-H: 7d945768331212eadca4a9c35390405efa72cf1be7f064cef170aa68cdab0be9; Copyright 1990-2023 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GBK2K-V: 49e81ae0d4b7b35901fbaeff63558bb133d9d0398ac2d0e33fab75404ea71cd2; Copyright 1990-2023 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GBKp-EUC-H: d6ef0e6c393bd065f5b646b7dc87fe8df88f3608a9264fb5727b552418352d5a; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GBKp-EUC-V: 43c8fa12dcef0b1b584fdd077996fff388fc0e9a9d332e08556640ceefa9fa0e; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GBpc-EUC-H: ec6132762b28650eb0c8483ea9387af39acb9d8aca432cb1e4f79a1058e1cd40; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/GBpc-EUC-V: 5fec79fd4f5f95d7638a150eec9b551268b8c29103446f0eee26866769ff384f; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/UniGB-UCS2-H: d2b100a68c016190347c6e0ecee3e7d6d1accd7395d7e803bf88b7ffef156725; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/UniGB-UCS2-V: d94d2e56a9a5cb89e0a68c31eba1ff40aa604b3a504be844c67de64af64eec96; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/UniGB-UTF16-H: 3750279409a96fb3a8ce1fbc25eb95bab4ff7ade023e4e0df6bd33258a3a8726; Copyright 1990-2023 Adobe. All rights reserved.
SHA-256 Adobe-GB1-6/UniGB-UTF16-V: cc9608b7c8c11133c1148eda883c10433e1d0dc2c964f41744eae89fe75f77d3; Copyright 1990-2023 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/83pv-RKSJ-H: 77f7a0f11fe2ee160baa6446b47b8a66cda41aac4ab777fb92c192198a521377; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/90ms-RKSJ-H: e0b104bcc8dfb3c2d3e025e12c59182d7e0ee1b5df6f55bc8550d4b3fc83ecd3; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/90ms-RKSJ-V: a588f7d20d0c8dbf09693480e62bf3ab80b7453858e4425d4b47650d48e7757b; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/90msp-RKSJ-H: 10b15c3b208d35f9b2764b0dd00e52de59170aa3f102387e7c388dfaa2290fc0; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/90msp-RKSJ-V: 05b5adf56096c0f1baeb267ec63f4018c85e3c3cff01f3457ffa10672a225c09; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/90pv-RKSJ-H: bda3f87262c61ef5a34097737f7306fb160d769f357b72ad251d11384a3848d2; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/90pv-RKSJ-V: 36ab76110635e300971cfeaaa9bc049e28ba3214d2a880dd3b97cf1a041cbca2; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/Add-RKSJ-H: 630d4689f3d17000208452c855d9360dc1f769f102b7d29c17ea11973b28aaa7; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/Add-RKSJ-V: 5664073e91e61009d72d5f007207f29bb8561c05939f3b185909ab92424bf039; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/EUC-H: 5cceeaf017e9f813079bbc17d8e750072e50042a01e521c9f9c18efabd596cba; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/EUC-V: d35c5ce976385980244040037152d92d321b738bca885ad0976c10658cd0acab; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/Ext-RKSJ-H: 00cd02206daf64e633d1c13b979fb20652a554d87fd9f2f0ca94065156ea8b71; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/Ext-RKSJ-V: 8a84926990144b043f2e5ebda194bf3560cc5f29096cafab8a6079a9d86e2d6b; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/H: f90323c3fd16e107b02c888167739f210af7144beba1bda83949069535db3346; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/UniJIS-UCS2-H: 05d064e6b373edf83890e26d7d2a2339482fad265eaf0a2b92ab528af33b5a54; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/UniJIS-UCS2-HW-H: 782f9d14dffa2a538302c8aa02b7d4650ff41d3bbe49215546c66a786be92bed; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/UniJIS-UCS2-HW-V: eb1fe9bd104770efb0fda13b211fee88cc5487a653e5cdf6d89bb782336e7673; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/UniJIS-UCS2-V: b487b2602348ef9c69222fd1a82cd45eea4f756d98e974b94183390c9bd8b6c3; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/UniJIS-UTF16-H: 5cf77ed38c25262dba845676738a887afe45e44425c3dd2489334c4d8c510fcb; Copyright 1990-2022 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/UniJIS-UTF16-V: 03bfb4e91452da537f347b8676e52adfa5dd10f8b920fa2cc34d343580354056; Copyright 1990-2022 Adobe. All rights reserved.
SHA-256 Adobe-Japan1-7/V: 1ccdbf3451794ddb929a9e2e43f562b20a8d9de5ec44174317c8a149ab498aa1; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/KSC-EUC-H: 90e9250027a7a84fc7a2542f91fe61e3a71299214aaa75c617e12a55dfaa0943; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/KSC-EUC-V: a51f69ff435ff83b3c200b2043572492be05ac08e98f4662f3938b3f6a1b9a6c; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/KSCms-UHC-H: f3e3719b2db01f1c27e5f8b85cfec0d6f018239fb999cd03bbe10084a9f4645a; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/KSCms-UHC-HW-H: 129c4892d7d21a5f188af469d06673994c9395bd61c33987a0652261d27a7f7b; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/KSCms-UHC-HW-V: b6052023250b12413fe7ac8552d64362720374df3c0d1beb2406922856a504fa; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/KSCms-UHC-V: 1ca4c9a90aa3079ffdc8ceb7e288f034d0afbc7142a7fae705416368fc581186; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/KSCpc-EUC-H: 6d2ec35d3aad68c4cba6d93849d8b7b9b8c34572c50b7361ccb6bf338c36f330; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/KSCpc-EUC-V: 2fc9b120724cea591b2ffd2bb4206a31329a085815e45d73e2b93ab5c1ac9f5a; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/UniKS-UCS2-H: bb28e3e84154f49c311128a5700cc0187ce4957b10f18dee88089002db7e5501; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/UniKS-UCS2-V: 76c08c4aace48d92a252e686f9d748ebde009337b59361ec286c4bdc037e5933; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/UniKS-UTF16-H: 0a1dc38d8dd4e1f55b5a3ab978a2b091109f88f4d08587bbb7864370cda8cfd5; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 Adobe-Korea1-2/UniKS-UTF16-V: 681aecd86956265504f7875af43eae3985ab263175aa782baa884b0f514f2aa6; Copyright 1990-2019 Adobe. All rights reserved.
SHA-256 LICENSE.md: 742665db9c8e1bc72603c6d319ca3e90b83bd6d95202f0cd4ef11068a07a9c29

Copyright 1990-2023 Adobe. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.

Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

Neither the name of Adobe nor the names of its contributors may be
used to endorse or promote products derived from this software without
specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
