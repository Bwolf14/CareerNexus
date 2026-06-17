# Sample resumes

This directory holds the integration-test corpus (blueprint §13).

Real resumes are **not** committed (PII + binary noise in git). Instead, the
integration tests (`test_parser.py`) synthesize representative samples into a
temp directory at runtime via `generate_samples.py`, covering:

| Sample                 | What it exercises                                  |
|------------------------|----------------------------------------------------|
| `single_column.pdf`    | Single-column PDF, bullets, dates, GPA, awards     |
| `two_column.pdf`       | Two-column / sidebar PDF (gutter column detection)  |
| `heading_styles.docx`  | DOCX using Word Heading/Title styles               |
| `manual_bold.docx`     | DOCX using manual **bold** formatting only         |
| `sidebar.docx`         | DOCX sidebar via a 2-column layout table           |
| `no_headers.docx`      | Plain prose, no headings -> `"failed"` status path |
| `scanned_like.pdf`     | Empty/image-only PDF -> `"failed"` status path     |

## Regenerate the samples here

To materialize the synthetic files in this folder (e.g. for manual
inspection or `python -m resume_parser <file>`):

```bash
python -m resume_parser.tests.generate_samples
```

The generated `*.pdf` / `*.docx` files are git-ignored.

## Adding real resumes

Drop real `.pdf` / `.docx` files in this folder to spot-check the parser
against them:

```bash
python -m resume_parser path/to/real_resume.pdf
```

They will be git-ignored, so no PII gets committed.
