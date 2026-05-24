# Anki Generator

Paste a link — get high-quality Anki flashcards. Built around Claude's vision API so
figures from articles, papers, and YouTube videos drive card creation alongside the
text.

## What it does

1. **Fetches content** from a URL (web article, PDF, YouTube video) or an uploaded PDF
2. **Extracts figures**:
   - Web pages: `<figure>` and `<img>` tags from the main content
   - PDFs: embedded images via PyMuPDF
   - YouTube: identifies key visual moments in the transcript → downloads the video →
     extracts frames at those timestamps → Claude curates the informative ones
3. **Generates cards** with Claude (vision-enabled). Figures are studied first; text
   fills in supporting facts. Each card may reference a source figure.
4. **Review in the browser**: remove cards you don't want; type a concept to get
   autofill suggestions.
5. **Saves to Anki**: figures are uploaded into Anki's media library and embedded
   directly into the cards.

## Setup

### 1. System dependencies

```bash
# macOS (Homebrew)
brew install ffmpeg  # required for YouTube frame extraction
```

You also need [Anki](https://apps.ankiweb.net/) with the
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) addon installed.
In Anki: Tools → Add-ons → Get Add-ons → enter code `2055492159` → restart Anki.

### 2. Python environment

```bash
git clone https://github.com/YOUR-USERNAME/anki-generator.git
cd anki-generator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. API key

Get an Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
and save it to your shell:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-api03-YOUR-KEY-HERE"' >> ~/.zshrc
source ~/.zshrc
```

## Usage

### Web portal (recommended)

```bash
python portal.py
```

A browser tab opens at <http://localhost:5005>. Paste a URL or upload a PDF, click
**Generate cards**, review/edit/add to your draft, then **Save to Anki**.

### CLI

```bash
python anki_gen.py https://en.wikipedia.org/wiki/Spaced_repetition
python anki_gen.py https://arxiv.org/abs/1706.03762 --deck "ML Papers"
python anki_gen.py https://www.youtube.com/watch?v=VIDEO_ID --deck "Talks"
```

## Card-style guidelines

Card style is governed by [`flashcard_guidelines.md`](./flashcard_guidelines.md).
Edit it freely — changes take effect on the next generation. The file ships with
research-backed principles (atomicity, minimum-information, avoid yes/no questions,
when to use Basic vs Cloze, etc.) drawn from SuperMemo, Andy Matuschak, and Michael
Nielsen's writings.

## Supported sources

| Source | Text | Figures |
|---|---|---|
| Web articles / blog posts | ✅ | ✅ `<figure>` and `<img>` tags |
| PDFs (URL or upload) | ✅ | ✅ embedded images via PyMuPDF |
| Arxiv (`/abs/` URLs auto-rewrite to PDF) | ✅ | ✅ |
| YouTube | ✅ transcript | ✅ frame extraction via yt-dlp + ffmpeg |

## Architecture

- **`anki_gen.py`** — content fetching, image processing, Claude API calls, card
  generation, AnkiConnect interaction. Also runnable as a standalone CLI.
- **`portal.py`** — Flask app providing the web portal. Wraps `anki_gen.py` and
  manages draft state for the review/commit flow.
- **`flashcard_guidelines.md`** — user-editable card style rules, injected into every
  Claude prompt.

## Project layout

```
anki-generator/
├── anki_gen.py              # core logic + CLI
├── portal.py                # Flask web portal
├── flashcard_guidelines.md  # editable card style rules
├── requirements.txt
└── README.md
```

## Acknowledgments

Card-design principles drawn from:
- [SuperMemo: Twenty Rules of Formulating Knowledge](https://supermemo.guru/wiki/20_rules_of_knowledge_formulation)
- [Andy Matuschak: How to Write Good Prompts](https://andymatuschak.org/prompts/)
- [Michael Nielsen: Augmenting Long-term Memory](https://augmentingcognition.com/ltm.html)

Built with [Claude](https://www.anthropic.com/claude), [AnkiConnect](https://github.com/FooSoft/anki-connect),
[PyMuPDF](https://pymupdf.readthedocs.io/), [yt-dlp](https://github.com/yt-dlp/yt-dlp),
and [Flask](https://flask.palletsprojects.com/).

## License

MIT — see [LICENSE](./LICENSE).
