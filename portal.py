#!/usr/bin/env python3
"""
portal.py — Local web portal for anki_gen, with figures-first card generation.

Flow:
  1. User submits URL → /draft. Server extracts text + figures, then asks
     Claude to build cards starting from the visuals. Cards reference the
     figure they relate to via `image_ref`.
  2. Browser renders the cards with thumbnails for figure-linked cards.
     User removes cards they don't want and types concepts in the "add card"
     input → /suggest returns vision-aware candidate cards.
  3. /commit uploads the referenced figures into Anki's media library and
     pushes the cards to Anki.

Run:
    python portal.py
"""

import base64
import os
import sys
import threading
import time
import uuid
import webbrowser

from flask import Flask, Response, jsonify, request

import anki_gen

app = Flask(__name__)

# ── In-memory draft store ──────────────────────────────────────────────────────

DRAFTS = {}  # draft_id -> {"content": {...}, "url", "deck", "guidelines",
             #             "source_kind", "created_at"}
DRAFT_TTL_SECONDS = 60 * 60  # 1 hour


def _gc_drafts():
    cutoff = time.time() - DRAFT_TTL_SECONDS
    for k in [k for k, v in DRAFTS.items() if v["created_at"] < cutoff]:
        d = DRAFTS.pop(k, None)
        if d:
            anki_gen.cleanup_content(d.get("content"))


def _image_data_url(img):
    """Build a `data:` URL for in-browser preview."""
    b64 = base64.b64encode(img["data"]).decode("ascii")
    return f"data:{img['mime']};base64,{b64}"


def _public_image_payload(images):
    """Compact, browser-friendly version of the images list (data URLs)."""
    return [
        {
            "data_url": _image_data_url(img),
            "caption": img.get("caption", ""),
            "source_kind": img.get("source_kind", ""),
        }
        for img in images
    ]


# ── HTML/CSS/JS (single page) ──────────────────────────────────────────────────

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Anki Generator</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #181b22;
    --panel-2: #1f232c;
    --border: #262a33;
    --text: #e6e8ee;
    --muted: #8b93a3;
    --accent: #6ea8ff;
    --accent-hover: #8ab8ff;
    --error: #ff7b7b;
    --success: #6fd29a;
    --danger: #ff7b7b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    justify-content: center;
    padding: 48px 20px 80px;
  }
  main { width: 100%; max-width: 760px; }
  h1 { font-size: 28px; font-weight: 600; margin: 0 0 8px; }
  .subtitle { color: var(--muted); margin: 0; font-size: 15px; }

  .header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 24px;
    margin-bottom: 32px;
  }
  .model-picker { display: flex; flex-direction: column; min-width: 180px; }
  .model-picker select {
    padding: 10px 12px;
    background: #0f1219;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 14px;
    font-family: inherit;
    cursor: pointer;
  }
  .model-picker select:focus { outline: none; border-color: var(--accent); }

  /* HTML5 datalist styling is browser-dependent; the input is the same */
  datalist { display: none; }
  h2 { font-size: 16px; font-weight: 600; margin: 28px 0 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }

  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
  }
  label {
    display: block;
    font-size: 13px;
    font-weight: 500;
    color: var(--muted);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  input[type=text] {
    width: 100%;
    padding: 12px 14px;
    background: #0f1219;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 15px;
    font-family: inherit;
    outline: none;
    transition: border-color 0.15s;
  }
  input[type=text]:focus { border-color: var(--accent); }

  button {
    padding: 11px 18px;
    background: var(--accent);
    color: #0a0d14;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s, opacity 0.15s;
    font-family: inherit;
  }
  button:hover:not(:disabled) { background: var(--accent-hover); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.full { width: 100%; padding: 13px; }
  button.ghost {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
  }
  button.ghost:hover:not(:disabled) { background: var(--panel-2); color: var(--text); }
  button.danger { background: transparent; color: var(--danger); border: 1px solid transparent; padding: 4px 8px; font-size: 13px; }
  button.danger:hover:not(:disabled) { background: rgba(255,123,123,0.1); }
  button.small {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
  }
  button.small:hover:not(:disabled) { background: var(--panel-2); color: var(--text); border-color: var(--accent); }
  button.primary-small {
    background: var(--accent);
    color: #0a0d14;
    padding: 4px 10px;
    font-size: 12px;
  }

  textarea.edit-field {
    width: 100%;
    padding: 8px 10px;
    background: #0f1219;
    border: 1px solid var(--accent);
    border-radius: 6px;
    color: var(--text);
    font-size: 14px;
    font-family: inherit;
    resize: vertical;
    min-height: 50px;
    margin: 4px 0;
  }
  textarea.edit-field:focus { outline: none; }

  #status, #commit-status { margin-top: 20px; font-size: 14px; min-height: 20px; }
  .status-working { color: var(--muted); }
  .status-success { color: var(--success); }
  .status-error { color: var(--error); }
  .spinner {
    display: inline-block; width: 12px; height: 12px;
    border: 2px solid var(--muted); border-top-color: transparent;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    margin-right: 8px; vertical-align: -2px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 10px;
    font-size: 14px;
    line-height: 1.5;
    display: flex;
    gap: 12px;
    align-items: flex-start;
  }
  .card.suggestion { background: var(--panel-2); border-style: dashed; }
  .card-body { flex: 1; min-width: 0; }
  .card-actions { display: flex; flex-direction: column; gap: 6px; flex-shrink: 0; }

  .card-image {
    max-width: 100%;
    max-height: 240px;
    border-radius: 6px;
    margin: 8px 0;
    background: #0a0d14;
    object-fit: contain;
    display: block;
  }
  .image-edit-wrap {
    position: relative;
    display: inline-block;
    margin: 8px 0;
  }
  .image-edit-wrap .card-image { margin: 0; }
  .image-remove-btn {
    position: absolute;
    top: 6px;
    right: 6px;
    width: 24px;
    height: 24px;
    padding: 0;
    border-radius: 50%;
    background: rgba(0, 0, 0, 0.75);
    color: white;
    border: 1px solid rgba(255, 255, 255, 0.2);
    font-size: 14px;
    line-height: 1;
    cursor: pointer;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .image-remove-btn:hover { background: var(--danger); color: white; border-color: var(--danger); }
  .paste-hint {
    color: var(--muted);
    font-size: 12px;
    font-style: italic;
    margin: 4px 0 8px;
  }

  .badge {
    display: inline-block;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 2px 8px;
    border-radius: 4px;
    margin-right: 8px;
    vertical-align: 1px;
  }
  .badge.basic { background: #2a3e5c; color: #aed1ff; }
  .badge.cloze { background: #3a2c52; color: #d4b6ff; }
  .badge.manual { background: #2d4a3a; color: #b6e4c8; }
  .badge.figure { background: #4a3a2c; color: #f0c89c; }

  .label-line { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; margin: 8px 0 4px; }
  .text-line { white-space: pre-wrap; word-wrap: break-word; }
  .text-muted { color: var(--muted); }
  .cloze-mark { color: var(--accent); font-weight: 600; }

  .hidden { display: none !important; }
  .add-card-area { margin-top: 16px; }
  .footer-actions {
    display: flex;
    gap: 10px;
    margin-top: 24px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
  }
  .footer-actions button { flex: 1; }
  .count-line { color: var(--muted); font-size: 13px; margin-bottom: 12px; }

  .figures-row {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding: 8px 0;
    margin-bottom: 8px;
  }
  .figures-row img {
    height: 70px;
    width: auto;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: #0a0d14;
    object-fit: contain;
    flex-shrink: 0;
  }
  .source-info {
    color: var(--muted);
    font-size: 13px;
    margin-bottom: 16px;
    padding: 8px 12px;
    background: var(--panel-2);
    border-radius: 6px;
  }
  .source-info strong { color: var(--text); }
  .warnings {
    margin-bottom: 16px;
    padding: 12px 14px;
    background: rgba(255, 200, 100, 0.08);
    border: 1px solid rgba(255, 200, 100, 0.3);
    border-radius: 8px;
    color: #f0c89c;
    font-size: 13px;
    line-height: 1.5;
  }
  .warnings strong { color: #ffb84d; }
  .warnings ul { margin: 6px 0 0 0; padding-left: 20px; }
  .warnings li { margin: 4px 0; }

  .seg-toggle {
    display: inline-flex;
    background: #0f1219;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 3px;
    gap: 2px;
    align-self: flex-start;
  }
  .seg-btn {
    background: transparent;
    color: var(--muted);
    border: none;
    padding: 6px 14px;
    font-size: 13px;
    font-weight: 500;
    border-radius: 6px;
    cursor: pointer;
    font-family: inherit;
    transition: background 0.12s, color 0.12s;
  }
  .seg-btn:hover:not(.active) { color: var(--text); background: var(--panel-2); }
  .seg-btn.active {
    background: var(--accent);
    color: #0a0d14;
  }

  .deck-mode-row { transition: opacity 0.15s; }
  .deck-mode-row.inactive { opacity: 0.45; }

  select#deck-existing-select {
    width: 100%;
    padding: 12px 14px;
    background: #0f1219;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 15px;
    font-family: inherit;
    cursor: pointer;
  }
  select#deck-existing-select:focus { outline: none; border-color: var(--accent); }

  .enriched-flash {
    margin: 8px 0;
    padding: 6px 10px;
    background: rgba(111, 210, 154, 0.1);
    border-left: 3px solid var(--success);
    border-radius: 4px;
    color: var(--success);
    font-size: 12px;
    font-style: italic;
    line-height: 1.4;
  }
</style>
</head>
<body>
<main>
  <div class="header-row">
    <div>
      <h1>Anki Generator</h1>
      <p class="subtitle">Paste a link. We extract figures + text, then build cards from the visuals.</p>
    </div>
    <div class="model-picker">
      <label for="model-select" style="margin-bottom: 4px;">Model</label>
      <select id="model-select"></select>
    </div>
  </div>

  <!-- Phase 1: URL form -->
  <div id="phase-input" class="panel">
    <div id="mode-url">
      <label for="url">URL (article, paper PDF, blog post, or YouTube video)</label>
      <input type="text" id="url" autofocus
             placeholder="https://example.com/some-article">
      <div style="margin-top: 8px; font-size: 13px;">
        <a href="#" id="switch-to-file" style="color: var(--accent); text-decoration: none;">
          or upload a PDF file instead &rarr;
        </a>
      </div>
    </div>

    <div id="mode-file" class="hidden">
      <label for="file">PDF file (download manually from sites that block scrapers)</label>
      <input type="file" id="file" accept="application/pdf,.pdf"
             style="padding: 12px 14px; background: #0f1219; border: 1px solid var(--border); border-radius: 8px; color: var(--text); width: 100%; font-family: inherit;">
      <div style="margin-top: 8px; font-size: 13px;">
        <a href="#" id="switch-to-url" style="color: var(--accent); text-decoration: none;">
          &larr; back to URL
        </a>
      </div>
    </div>

    <button id="generate-btn" class="full" style="margin-top: 20px;">Generate cards</button>
    <div id="status"></div>
  </div>

  <!-- Phase 2: Review -->
  <div id="phase-review" class="hidden">
    <div class="panel" style="margin-bottom: 20px; display: flex; flex-direction: column; gap: 10px;">
      <label>Save to deck</label>
      <div class="seg-toggle" role="tablist">
        <button type="button" id="mode-new-btn" class="seg-btn active" data-mode="new" role="tab">+ New deck</button>
        <button type="button" id="mode-existing-btn" class="seg-btn" data-mode="existing" role="tab">↪ Existing deck</button>
      </div>

      <div id="deck-mode-new" class="deck-mode-row">
        <!-- autocomplete=new-password + unusual name + data-form-type=other defeats
             Chrome/Safari's heuristic autofill, which ignores autocomplete=off. -->
        <input type="text"
               id="deck-new-name"
               name="ankigen-deck-name-xyz"
               autocomplete="new-password"
               autocorrect="off"
               autocapitalize="off"
               spellcheck="false"
               data-lpignore="true"
               data-form-type="other"
               placeholder="(suggestion from source will appear here)">
      </div>

      <div id="deck-mode-existing" class="deck-mode-row">
        <select id="deck-existing-select">
          <option value="">— pick an existing deck —</option>
        </select>
      </div>

      <div id="deck-merge-hint" style="font-size: 12px; color: var(--muted); min-height: 16px;"></div>
    </div>

    <div id="source-info" class="source-info"></div>
    <div id="warnings-box"></div>
    <div id="figures-strip"></div>

    <h2>Review cards <span id="card-count" class="text-muted" style="text-transform:none; font-weight:400;"></span></h2>
    <div class="count-line">Click <strong>Remove</strong> to drop a card. Add more below.</div>
    <div id="cards-list"></div>

    <h2>Add a card</h2>
    <div class="panel add-card-area">
      <label for="concept">Concept or topic</label>
      <input type="text" id="concept"
             placeholder="Start typing — e.g. 'spacing effect', 'why retrieval works'…">
      <div id="suggest-status" style="font-size:13px; color:var(--muted); margin-top:10px; min-height:18px;"></div>
      <div id="suggestions" style="margin-top: 12px;"></div>
    </div>

    <div class="footer-actions">
      <button id="discard-btn" class="ghost">Discard &amp; start over</button>
      <button id="commit-btn">Save to Anki</button>
    </div>
    <div id="commit-status"></div>
  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);

// ── State ────────────────────────────────────────────────────────────────────
let state = {
  draftId: null,
  deck: 'AI Generated',
  sourceKind: 'web',
  model: localStorage.getItem('anki_gen_model') || 'claude-opus-4-5',
  cards: [],          // {type, ..., image_ref, _source, _editing?, _rerolling?, _enriching?}
  suggestions: [],    // candidate cards from /suggest
  images: [],         // [{data_url, caption, source_kind}]
  warnings: [],
  existingDecks: [],  // names of decks already in Anki (for merge detection)
};

// ── One-shot bootstrap: load models + decks ──────────────────────────────────
async function loadModels() {
  try {
    const r = await fetch('/models');
    const data = await r.json();
    const sel = $('model-select');
    sel.innerHTML = '';
    data.models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = `${m.label} — ${m.tier}`;
      sel.appendChild(opt);
    });
    sel.value = state.model;
    sel.addEventListener('change', () => {
      state.model = sel.value;
      localStorage.setItem('anki_gen_model', state.model);
    });
  } catch (e) { /* model picker is optional */ }
}

async function loadDecks() {
  try {
    const r = await fetch('/decks');
    const data = await r.json();
    state.existingDecks = data.decks || [];
    const sel = $('deck-existing-select');
    sel.innerHTML = '<option value="">— pick a deck —</option>';
    state.existingDecks.forEach(name => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
    // If user already has no existing decks, hide the "existing" tab option entirely
    if (!state.existingDecks.length) {
      const btn = $('mode-existing-btn');
      if (btn) btn.disabled = true;
      if (btn) btn.title = 'No existing decks in Anki yet';
    }
  } catch (e) { /* deck picker is optional */ }
}

// Which mode is active: 'new' or 'existing'. Tracked separately from the
// input values so switching tabs preserves what the user typed/picked.
let deckMode = 'new';

function setDeckMode(mode) {
  deckMode = mode;
  $('mode-new-btn').classList.toggle('active', mode === 'new');
  $('mode-existing-btn').classList.toggle('active', mode === 'existing');
  $('deck-mode-new').classList.toggle('inactive', mode !== 'new');
  $('deck-mode-existing').classList.toggle('inactive', mode !== 'existing');
  updateDeckHint();
}

function getChosenDeck() {
  if (deckMode === 'existing') {
    return ($('deck-existing-select').value || '').trim();
  }
  return ($('deck-new-name').value || '').trim();
}

function updateDeckHint() {
  const hint = $('deck-merge-hint');
  if (!hint) return;
  const name = getChosenDeck();
  if (deckMode === 'existing') {
    if (!name) {
      hint.textContent = 'Choose a deck from the list above.';
      hint.style.color = 'var(--muted)';
    } else {
      hint.textContent = `↪ Cards will be added to "${name}"`;
      hint.style.color = 'var(--accent)';
    }
  } else {
    if (!name) {
      hint.textContent = 'Type a name for the new deck.';
      hint.style.color = 'var(--muted)';
    } else if (state.existingDecks.includes(name)) {
      hint.textContent = `↪ A deck named "${name}" already exists — cards will be merged into it`;
      hint.style.color = 'var(--accent)';
    } else {
      hint.textContent = `+ A new deck "${name}" will be created`;
      hint.style.color = 'var(--muted)';
    }
  }
}
loadModels();
loadDecks();

// ── Helpers ──────────────────────────────────────────────────────────────────
function setStatus(elId, html, cls) {
  const el = $(elId);
  el.className = cls ? `status-${cls}` : '';
  el.innerHTML = html;
}

function renderClozeText(text) {
  return text.replace(/\{\{c\d+::([^}]+?)(?:::[^}]+?)?\}\}/g,
    '<span class="cloze-mark">[$1]</span>');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function cardImageHTML(card) {
  if (card.image_ref === null || card.image_ref === undefined) return '';
  const img = state.images[card.image_ref];
  if (!img) return '';
  return `<img class="card-image" src="${img.data_url}" alt="${escapeHtml(img.caption || '')}">`;
}

function cardDisplayHTML(card) {
  const sourceBadge = card._source === 'manual'
    ? '<span class="badge manual">Manual</span>' : '';
  const figureBadge = (card.image_ref !== null && card.image_ref !== undefined)
    ? `<span class="badge figure">Fig ${card.image_ref}</span>` : '';
  const enrichedFlash = card._enriched_msg
    ? `<div class="enriched-flash">✓ ${escapeHtml(card._enriched_msg)}</div>` : '';
  if ((card.type || 'basic') === 'cloze') {
    return `
      ${sourceBadge}
      <span class="badge cloze">Cloze</span>
      ${figureBadge}
      ${enrichedFlash}
      ${cardImageHTML(card)}
      <div class="label-line">Text</div>
      <div class="text-line">${renderClozeText(escapeHtml(card.text || ''))}</div>
      ${card.extra ? `<div class="label-line">Back Extra</div><div class="text-line text-muted">${escapeHtml(card.extra)}</div>` : ''}
    `;
  }
  return `
    ${sourceBadge}
    <span class="badge basic">Basic</span>
    ${figureBadge}
    ${enrichedFlash}
    ${cardImageHTML(card)}
    <div class="label-line">Question</div>
    <div class="text-line">${escapeHtml(card.question || '')}</div>
    <div class="label-line">Answer</div>
    <div class="text-line text-muted">${escapeHtml(card.answer || '')}</div>
  `;
}

function cardEditImageHTML(card, idx) {
  // In edit mode, wrap the image with a removable × button.
  if (card.image_ref === null || card.image_ref === undefined) return '';
  const img = state.images[card.image_ref];
  if (!img) return '';
  return `
    <div class="image-edit-wrap">
      <img class="card-image" src="${img.data_url}" alt="${escapeHtml(img.caption || '')}">
      <button class="image-remove-btn" data-action="remove-image" data-idx="${idx}" title="Remove image">×</button>
    </div>
  `;
}

function cardEditHTML(card, idx) {
  const sourceBadge = card._source === 'manual'
    ? '<span class="badge manual">Manual</span>' : '';
  const figureBadge = (card.image_ref !== null && card.image_ref !== undefined)
    ? `<span class="badge figure">Fig ${card.image_ref}</span>` : '';
  const hasImage = (card.image_ref !== null && card.image_ref !== undefined);
  const pasteHint = `<div class="paste-hint">💡 ${hasImage ? 'Paste an image (⌘V) below to replace the current one' : 'Paste an image (⌘V) anywhere below to attach one to this card'}</div>`;
  if ((card.type || 'basic') === 'cloze') {
    return `
      ${sourceBadge}
      <span class="badge cloze">Cloze</span>
      ${figureBadge}
      ${cardEditImageHTML(card, idx)}
      ${pasteHint}
      <div class="label-line">Text (use {{c1::word}} for cloze deletions)</div>
      <textarea class="edit-field" data-field="text" data-idx="${idx}">${escapeHtml(card.text || '')}</textarea>
      <div class="label-line">Back Extra (optional)</div>
      <textarea class="edit-field" data-field="extra" data-idx="${idx}">${escapeHtml(card.extra || '')}</textarea>
    `;
  }
  return `
    ${sourceBadge}
    <span class="badge basic">Basic</span>
    ${figureBadge}
    ${cardEditImageHTML(card, idx)}
    ${pasteHint}
    <div class="label-line">Question</div>
    <textarea class="edit-field" data-field="question" data-idx="${idx}">${escapeHtml(card.question || '')}</textarea>
    <div class="label-line">Answer</div>
    <textarea class="edit-field" data-field="answer" data-idx="${idx}">${escapeHtml(card.answer || '')}</textarea>
  `;
}

function cardActionsHTML(card, idx) {
  if (card._rerolling) {
    return `<div style="color: var(--muted); font-size: 12px;"><span class="spinner"></span>Re-rolling…</div>`;
  }
  if (card._enriching) {
    return `<div style="color: var(--muted); font-size: 12px;"><span class="spinner"></span>Enriching…</div>`;
  }
  if (card._editing) {
    return `
      <button class="primary-small" data-action="save-edit" data-idx="${idx}">Save</button>
      <button class="small" data-action="cancel-edit" data-idx="${idx}">Cancel</button>
    `;
  }
  return `
    <button class="small" data-action="edit" data-idx="${idx}">Edit</button>
    <button class="small" data-action="reroll" data-idx="${idx}">↻ Re-roll</button>
    <button class="small" data-action="enrich" data-idx="${idx}" title="Hunt for a visual that matches this card">✨ Enrich</button>
    <button class="danger" data-action="remove" data-idx="${idx}">Remove</button>
  `;
}

function renderWarnings() {
  const box = $('warnings-box');
  if (!state.warnings || !state.warnings.length) { box.innerHTML = ''; return; }
  const items = state.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('');
  box.innerHTML = `<div class="warnings"><strong>Visuals are core to good cards — but extraction had issues:</strong><ul>${items}</ul>Tip: click <strong>✨ Enrich</strong> on any card to actively hunt for a relevant visual.</div>`;
}

function renderCards() {
  const list = $('cards-list');
  list.innerHTML = '';
  state.cards.forEach((card, idx) => {
    const div = document.createElement('div');
    div.className = 'card';
    const body = card._editing ? cardEditHTML(card, idx) : cardDisplayHTML(card);
    div.innerHTML = `
      <div class="card-body">${body}</div>
      <div class="card-actions">${cardActionsHTML(card, idx)}</div>
    `;
    list.appendChild(div);
  });
  $('card-count').textContent = state.cards.length
    ? `(${state.cards.length})` : '(none)';
}

function renderSuggestions() {
  const wrap = $('suggestions');
  wrap.innerHTML = '';
  state.suggestions.forEach((card, idx) => {
    const div = document.createElement('div');
    div.className = 'card suggestion';
    div.innerHTML = `
      <div class="card-body">${cardDisplayHTML(card)}</div>
      <div class="card-actions">
        <button data-action="add-suggestion" data-idx="${idx}">+ Add</button>
      </div>
    `;
    wrap.appendChild(div);
  });
}

function renderFigureStrip() {
  const strip = $('figures-strip');
  strip.innerHTML = '';
  if (!state.images.length) return;
  const row = document.createElement('div');
  row.className = 'figures-row';
  state.images.forEach((img, i) => {
    const el = document.createElement('img');
    el.src = img.data_url;
    el.title = `Figure ${i}: ${img.caption || '(no caption)'}`;
    row.appendChild(el);
  });
  const label = document.createElement('div');
  label.className = 'label-line';
  label.textContent = `Extracted figures (${state.images.length})`;
  strip.appendChild(label);
  strip.appendChild(row);
}

function renderSourceInfo() {
  const counts = {
    basic: state.cards.filter(c => (c.type || 'basic') === 'basic').length,
    cloze: state.cards.filter(c => c.type === 'cloze').length,
    withFig: state.cards.filter(c => c.image_ref !== null && c.image_ref !== undefined).length,
  };
  let kindNote = '';
  if (state.sourceKind === 'youtube') {
    kindNote = ' &middot; <em>YouTube: transcript only (no frame extraction yet)</em>';
  } else if (state.sourceKind === 'pdf') {
    kindNote = ' &middot; PDF';
  }
  $('source-info').innerHTML =
    `Source: <strong>${state.sourceKind}</strong>${kindNote} &middot; ` +
    `${state.images.length} figure(s) &middot; ${state.cards.length} cards ` +
    `(${counts.basic} basic, ${counts.cloze} cloze, ${counts.withFig} with figure)`;
}

// ── Mode toggle (URL ↔ file) ─────────────────────────────────────────────────
let inputMode = 'url';  // 'url' | 'file'
$('switch-to-file').addEventListener('click', (e) => {
  e.preventDefault();
  inputMode = 'file';
  $('mode-url').classList.add('hidden');
  $('mode-file').classList.remove('hidden');
  $('file').focus();
});
$('switch-to-url').addEventListener('click', (e) => {
  e.preventDefault();
  inputMode = 'url';
  $('mode-file').classList.add('hidden');
  $('mode-url').classList.remove('hidden');
  $('url').focus();
});

// ── Event handlers ───────────────────────────────────────────────────────────
$('generate-btn').addEventListener('click', async () => {
  // The deck name is now chosen AFTER generation, on the review panel.
  // Send a placeholder; the real deck will be set at /commit time.
  const deck = 'AI Generated';
  let request;
  let etaLabel;

  if (inputMode === 'file') {
    const fileInput = $('file');
    const file = fileInput.files && fileInput.files[0];
    if (!file) { setStatus('status', 'Please select a PDF file.', 'error'); return; }
    const form = new FormData();
    form.append('file', file);
    form.append('deck', deck);
    form.append('model', state.model);
    request = { method: 'POST', body: form };  // no Content-Type, browser sets boundary
    etaLabel = `${Math.ceil(file.size / 1e6)} MB PDF · 30–90s`;
  } else {
    const url = $('url').value.trim();
    if (!url) { setStatus('status', 'Please enter a URL.', 'error'); return; }
    const isYoutube = /(?:youtube\.com|youtu\.be)/i.test(url);
    etaLabel = isYoutube ? '1–3 min (downloading video + extracting frames)' : '30–90s';
    request = {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, deck, model: state.model}),
    };
  }

  $('generate-btn').disabled = true;
  setStatus('status',
    `<span class="spinner"></span>Fetching content and generating cards — ${etaLabel}…`,
    'working');

  try {
    const r = await fetch('/draft', request);
    const data = await r.json();
    if (!r.ok || data.error) {
      setStatus('status', 'Error: ' + (data.error || 'Unknown'), 'error');
      $('generate-btn').disabled = false;
      return;
    }
    state.draftId = data.draft_id;
    state.deck = data.suggested_deck || data.deck || 'AI Generated';
    state.sourceKind = data.source_kind || 'web';
    state.images = data.images || [];
    state.warnings = data.warnings || [];
    state.cards = (data.cards || []).map(c => ({...c, _source: 'auto'}));
    console.log('[ankigen] /draft returned suggested_deck =', JSON.stringify(data.suggested_deck),
                ' | state.deck =', JSON.stringify(state.deck));
    $('phase-input').classList.add('hidden');
    $('phase-review').classList.remove('hidden');
    // Set the deck input value AFTER the panel is visible. Doing it before
    // can race with browser form autofill on some browsers. Also re-set
    // after a microtask + a small timeout to defeat late autofill writes.
    const setDeck = () => {
      const el = $('deck-new-name');
      el.value = state.deck;
      setDeckMode('new');
      console.log('[ankigen] deck-new-name.value set to:', JSON.stringify(el.value));
    };
    setDeck();
    requestAnimationFrame(setDeck);
    setTimeout(setDeck, 250);
    renderFigureStrip();
    renderWarnings();
    renderCards();
    renderSourceInfo();
    setStatus('status', '', '');
  } catch (e) {
    setStatus('status', 'Network error: ' + e.message, 'error');
    $('generate-btn').disabled = false;
  }
});

document.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const idx = parseInt(btn.dataset.idx, 10);

  if (action === 'remove') {
    state.cards.splice(idx, 1);
    renderCards();
    renderSourceInfo();
  } else if (action === 'add-suggestion') {
    const card = state.suggestions[idx];
    if (!card) return;
    state.cards.push({...card, _source: 'manual'});
    state.suggestions.splice(idx, 1);
    renderCards();
    renderSuggestions();
    renderSourceInfo();
  } else if (action === 'edit') {
    state.cards[idx]._editing = true;
    state.cards[idx]._editBackup = {
      question:  state.cards[idx].question,
      answer:    state.cards[idx].answer,
      text:      state.cards[idx].text,
      extra:     state.cards[idx].extra,
      image_ref: state.cards[idx].image_ref,
    };
    renderCards();
  } else if (action === 'cancel-edit') {
    const c = state.cards[idx];
    if (c._editBackup) Object.assign(c, c._editBackup);
    delete c._editing;
    delete c._editBackup;
    renderCards();
  } else if (action === 'save-edit') {
    // Pull all textarea values for this card into state.cards[idx]
    document.querySelectorAll(`textarea.edit-field[data-idx="${idx}"]`).forEach(t => {
      state.cards[idx][t.dataset.field] = t.value;
    });
    delete state.cards[idx]._editing;
    delete state.cards[idx]._editBackup;
    renderCards();
  } else if (action === 'reroll') {
    rerollCard(idx);
  } else if (action === 'enrich') {
    enrichCard(idx);
  } else if (action === 'remove-image') {
    state.cards[idx].image_ref = null;
    renderCards();
  }
});

// ── Paste-to-attach-image: works on textareas inside an editing card ─────────
document.addEventListener('paste', async (e) => {
  const target = e.target;
  if (!(target instanceof HTMLTextAreaElement) || !target.classList.contains('edit-field')) {
    return;  // not in our edit textarea
  }
  const items = e.clipboardData ? e.clipboardData.items : null;
  if (!items) return;
  for (const item of items) {
    if (item.type && item.type.startsWith('image/')) {
      e.preventDefault();  // stop the text-paste behaviour
      const blob = item.getAsFile();
      if (!blob) return;
      const idx = parseInt(target.dataset.idx, 10);
      await uploadPastedImage(blob, idx);
      return;
    }
  }
});

async function uploadPastedImage(blob, cardIdx) {
  const form = new FormData();
  form.append('file', blob, 'pasted.' + (blob.type.split('/')[1] || 'png'));
  form.append('draft_id', state.draftId);
  try {
    const r = await fetch('/upload_image', { method: 'POST', body: form });
    const data = await r.json();
    if (!r.ok || data.error) {
      alert('Image upload failed: ' + (data.error || 'Unknown error'));
      return;
    }
    state.images.push(data.image);
    if (state.cards[cardIdx]) {
      state.cards[cardIdx].image_ref = data.image_index;
    }
    renderFigureStrip();
    renderCards();  // re-renders the edit form with the new image attached
  } catch (e) {
    alert('Network error: ' + e.message);
  }
}

async function enrichCard(idx) {
  const card = state.cards[idx];
  if (!card) return;
  state.cards[idx]._enriching = true;
  renderCards();
  try {
    const r = await fetch('/enrich', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        draft_id: state.draftId,
        model: state.model,
        card: stripInternal(card),
      }),
    });
    const data = await r.json();
    delete state.cards[idx]._enriching;

    // Failure: clear error alert
    if (!r.ok || data.error || !data.card) {
      alert('Enrich failed: ' + (data.error || data.message || 'Unknown error'));
      renderCards();
      return;
    }

    // Success: silently update the card. The new image + caption tell the
    // story; no alert popup. We do flash a brief status note inline.
    if (data.new_image) state.images.push(data.new_image);
    state.cards[idx] = {...data.card, _source: card._source || 'auto', _enriched_msg: data.message};
    renderFigureStrip();
    renderCards();
    renderSourceInfo();
    // Auto-clear the status flash after a few seconds
    setTimeout(() => {
      if (state.cards[idx]) {
        delete state.cards[idx]._enriched_msg;
        renderCards();
      }
    }, 6000);
  } catch (e) {
    delete state.cards[idx]._enriching;
    alert('Network error: ' + e.message);
    renderCards();
  }
}

async function rerollCard(idx) {
  const original = state.cards[idx];
  if (!original) return;
  state.cards[idx]._rerolling = true;
  renderCards();
  try {
    const r = await fetch('/reroll', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        draft_id: state.draftId,
        model: state.model,
        // Strip our internal flags before sending
        card: stripInternal(original),
        other_cards: state.cards.filter((_, i) => i !== idx).map(stripInternal),
      }),
    });
    const data = await r.json();
    if (!r.ok || data.error || !data.card) {
      alert('Re-roll failed: ' + (data.error || 'Unknown error'));
      delete state.cards[idx]._rerolling;
      renderCards();
      return;
    }
    // Preserve the _source tag from the original (manual stays manual, etc.)
    state.cards[idx] = {...data.card, _source: original._source || 'auto'};
  } catch (e) {
    alert('Network error: ' + e.message);
    delete state.cards[idx]._rerolling;
  }
  renderCards();
  renderSourceInfo();
}

function stripInternal(card) {
  const { _source, _editing, _editBackup, _rerolling, _enriching, _enriched_msg, ...rest } = card;
  return rest;
}

let suggestTimer = null;
let suggestSeq = 0;
$('concept').addEventListener('input', () => {
  clearTimeout(suggestTimer);
  const concept = $('concept').value.trim();
  if (concept.length < 3) {
    state.suggestions = [];
    renderSuggestions();
    $('suggest-status').textContent = '';
    return;
  }
  suggestTimer = setTimeout(() => fetchSuggestions(concept), 600);
});

async function fetchSuggestions(concept) {
  const mySeq = ++suggestSeq;
  $('suggest-status').innerHTML = '<span class="spinner"></span>Generating suggestions…';
  try {
    const r = await fetch('/suggest', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        draft_id: state.draftId,
        model: state.model,
        concept,
        existing_cards: state.cards.map(stripInternal),
      }),
    });
    const data = await r.json();
    if (mySeq !== suggestSeq) return;
    if (!r.ok || data.error) {
      $('suggest-status').innerHTML =
        `<span style="color:var(--error)">Error: ${data.error || 'Unknown'}</span>`;
      state.suggestions = [];
    } else {
      state.suggestions = data.suggestions || [];
      $('suggest-status').textContent = state.suggestions.length
        ? `${state.suggestions.length} suggestion${state.suggestions.length === 1 ? '' : 's'}:`
        : 'No suggestions — try a different phrasing.';
    }
    renderSuggestions();
  } catch (e) {
    if (mySeq !== suggestSeq) return;
    $('suggest-status').innerHTML =
      `<span style="color:var(--error)">Network error: ${e.message}</span>`;
  }
}

$('commit-btn').addEventListener('click', async () => {
  if (!state.cards.length) {
    setStatus('commit-status', 'No cards to save.', 'error');
    return;
  }
  // Re-read the deck name from whichever mode is active
  const deckChosen = getChosenDeck() || 'AI Generated';
  if (deckMode === 'existing' && !getChosenDeck()) {
    setStatus('commit-status', 'Please pick an existing deck, or switch to "+ New deck".', 'error');
    return;
  }
  state.deck = deckChosen;
  $('commit-btn').disabled = true;
  setStatus('commit-status', '<span class="spinner"></span>Saving cards and figures to Anki…', 'working');
  try {
    const r = await fetch('/commit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        draft_id: state.draftId,
        deck: deckChosen,
        cards: state.cards.map(stripInternal),
      }),
    });
    const data = await r.json();
    if (!r.ok || data.error) {
      setStatus('commit-status', 'Error: ' + (data.error || 'Unknown'), 'error');
      $('commit-btn').disabled = false;
    } else {
      setStatus('commit-status',
        `✓ Saved ${data.added} cards to <strong>${escapeHtml(data.deck)}</strong>` +
        (data.skipped ? ` (${data.skipped} duplicates skipped)` : '') +
        '. <a href="#" id="new-link" style="color:var(--accent);">Generate another →</a>',
        'success');
      $('new-link').addEventListener('click', (e) => { e.preventDefault(); resetToInput(); });
    }
  } catch (e) {
    setStatus('commit-status', 'Network error: ' + e.message, 'error');
    $('commit-btn').disabled = false;
  }
});

$('mode-new-btn').addEventListener('click', () => setDeckMode('new'));
$('mode-existing-btn').addEventListener('click', () => {
  if ($('mode-existing-btn').disabled) return;
  setDeckMode('existing');
});
// Auto-switch the active mode based on which field the user is actually using.
$('deck-new-name').addEventListener('input', () => setDeckMode('new'));
$('deck-new-name').addEventListener('focus', () => setDeckMode('new'));
$('deck-existing-select').addEventListener('change', () => {
  if ($('deck-existing-select').value) setDeckMode('existing');
  else updateDeckHint();
});
$('deck-existing-select').addEventListener('focus', () => {
  if ($('mode-existing-btn').disabled) return;
  setDeckMode('existing');
});

$('discard-btn').addEventListener('click', () => {
  if (confirm('Discard these cards and start over?')) resetToInput();
});

function resetToInput() {
  state = {
    draftId: null,
    deck: 'AI Generated',
    sourceKind: 'web',
    model: $('model-select').value || localStorage.getItem('anki_gen_model') || 'claude-opus-4-5',
    cards: [], suggestions: [], images: [], warnings: [],
  };
  $('phase-review').classList.add('hidden');
  $('phase-input').classList.remove('hidden');
  $('url').value = '';
  $('concept').value = '';
  $('deck-new-name').value = '';
  $('deck-existing-select').value = '';
  $('deck-merge-hint').textContent = '';
  setDeckMode('new');
  $('suggestions').innerHTML = '';
  $('figures-strip').innerHTML = '';
  $('warnings-box').innerHTML = '';
  $('suggest-status').textContent = '';
  setStatus('status', '', '');
  setStatus('commit-status', '', '');
  $('generate-btn').disabled = false;
  $('commit-btn').disabled = false;
  $('url').focus();
}
</script>
</body>
</html>
"""


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/decks", methods=["GET"])
def decks():
    """Return the user's existing Anki decks for the deck-picker dropdown."""
    try:
        names = anki_gen.anki_request("deckNames")
    except Exception as e:
        return jsonify({"decks": [], "error": str(e)})
    # Skip the Default deck (Anki includes it but most users don't want it as the target)
    return jsonify({"decks": sorted(n for n in names if n != "Default")})


@app.route("/models", methods=["GET"])
def models():
    """Return the available Claude models for the model-picker dropdown."""
    return jsonify({
        "models": [
            {"id": name, **info}
            for name, info in anki_gen.AVAILABLE_MODELS.items()
        ],
        "default": anki_gen.DEFAULT_MODEL,
    })


@app.route("/draft", methods=["POST"])
def draft():
    """Generate cards from either a URL (JSON) or an uploaded PDF (multipart)."""
    # ── PDF file upload branch ────────────────────────────────────────────────
    if "file" in request.files:
        f = request.files["file"]
        if not f or not f.filename:
            return jsonify({"error": "No file uploaded."}), 400
        deck = anki_gen.normalize_deck_name(request.form.get("deck"))
        model = anki_gen.resolve_model(request.form.get("model"))
        try:
            pdf_bytes = f.read()
            content = anki_gen.fetch_pdf_from_bytes(pdf_bytes, source_label=f.filename)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        source_kind = "pdf"
        url = f"file://{f.filename}"
        return _finish_draft(content, source_kind, url, deck, model)

    # ── URL branch ────────────────────────────────────────────────────────────
    data = request.get_json(silent=True) or {}
    raw_url = data.get("url")
    url = (raw_url if isinstance(raw_url, str) else "").strip()
    deck = anki_gen.normalize_deck_name(data.get("deck"))
    model = anki_gen.resolve_model(data.get("model"))

    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        content, source_kind = anki_gen.fetch_content(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return _finish_draft(content, source_kind, url, deck, model)


def _finish_draft(content, source_kind, url, deck, model):
    """Shared post-fetch logic: validate content, generate cards, store draft."""
    n_chars = len(content.get("text", "").strip())
    n_imgs = len(content.get("images", []))
    if n_chars < 200 and n_imgs == 0:
        return jsonify({
            "error": (
                f"Could not extract usable content "
                f"(got {n_chars} chars of text, 0 figures). "
                "The page may be paywalled, require JavaScript to render, "
                "or be a landing page rather than the article itself. "
                "Try linking directly to the PDF or use the Upload PDF option."
            )
        }), 400

    try:
        guidelines = anki_gen.load_guidelines()
        cards = anki_gen.generate_cards(
            content, url, guidelines, source_kind=source_kind, model=model,
        )
    except Exception as e:
        return jsonify({"error": f"Card generation failed: {e}"}), 500

    if not cards:
        return jsonify({
            "error": (
                f"Claude returned 0 cards from the extracted content "
                f"({n_chars} chars, {n_imgs} figures). "
                "This usually means the content was too short or unstructured."
            )
        }), 400

    _gc_drafts()
    draft_id = uuid.uuid4().hex
    DRAFTS[draft_id] = {
        "content": content,
        "url": url,
        "deck": deck,
        "guidelines": guidelines,
        "source_kind": source_kind,
        "model": model,
        "created_at": time.time(),
    }
    return jsonify({
        "draft_id": draft_id,
        "deck": deck,
        "suggested_deck": anki_gen.normalize_deck_name(content.get("title"), default=""),
        "source_kind": source_kind,
        "model": model,
        "cards": cards,
        "images": _public_image_payload(content.get("images", [])),
        "warnings": content.get("warnings", []),
    })


@app.route("/suggest", methods=["POST"])
def suggest():
    """Vision-aware autofill for a user-typed concept."""
    data = request.get_json(silent=True) or {}
    draft_id = data.get("draft_id")
    raw_concept = data.get("concept")
    concept = (raw_concept if isinstance(raw_concept, str) else "").strip()

    if not draft_id or draft_id not in DRAFTS:
        return jsonify({"error": "Draft expired. Please start over."}), 400
    if len(concept) < 3:
        return jsonify({"suggestions": []})

    d = DRAFTS[draft_id]
    n_images = len(d["content"].get("images", []))
    # Filter malformed entries (None / non-dict) before passing to Claude
    existing = anki_gen.validate_cards(data.get("existing_cards") or [], n_images=n_images)
    model = anki_gen.resolve_model(data.get("model") or d.get("model"))
    try:
        suggestions = anki_gen.suggest_cards(
            d["content"], d["url"], d["guidelines"], concept, existing,
            source_kind=d.get("source_kind", "web"), model=model,
        )
        return jsonify({"suggestions": suggestions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reroll", methods=["POST"])
def reroll():
    """Generate a single replacement card on the same concept as the original."""
    data = request.get_json(silent=True) or {}
    draft_id = data.get("draft_id")
    original = data.get("card")
    other_cards = data.get("other_cards") or []

    if not draft_id or draft_id not in DRAFTS:
        return jsonify({"error": "Draft expired. Please start over."}), 400
    if not isinstance(original, dict):
        return jsonify({"error": "No valid card to re-roll."}), 400

    d = DRAFTS[draft_id]
    n_images = len(d["content"].get("images", []))
    original = anki_gen.validate_card(original, n_images=n_images)
    other_cards = anki_gen.validate_cards(other_cards, n_images=n_images)
    if original is None:
        return jsonify({"error": "Original card is malformed."}), 400

    model = anki_gen.resolve_model(data.get("model") or d.get("model"))
    try:
        new_card = anki_gen.reroll_card(
            d["content"], d["url"], d["guidelines"],
            original, other_cards,
            source_kind=d.get("source_kind", "web"), model=model,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if new_card is None:
        return jsonify({"error": "Claude returned an unusable card."}), 500
    return jsonify({"card": new_card})


@app.route("/commit", methods=["POST"])
def commit():
    """Upload referenced figures into Anki's media library and push cards."""
    data = request.get_json(silent=True) or {}
    draft_id = data.get("draft_id")
    deck = anki_gen.normalize_deck_name(data.get("deck"))

    if not draft_id or draft_id not in DRAFTS:
        return jsonify({"error": "Draft expired. Please start over."}), 400

    d = DRAFTS[draft_id]
    images = d["content"].get("images", [])
    # Validate + normalize cards from the browser: drops None/non-dict entries,
    # coerces image_ref to int, clamps out-of-range refs to None.
    cards = anki_gen.validate_cards(data.get("cards") or [], n_images=len(images))
    if not cards:
        return jsonify({"error": "No valid cards to save."}), 400

    try:
        anki_gen.ensure_deck(deck)
    except Exception as e:
        return jsonify({
            "error": (
                "Cannot reach Anki. Make sure Anki is open and the "
                "AnkiConnect addon (code 2055492159) is installed. "
                f"Details: {e}"
            )
        }), 500

    try:
        added, skipped = anki_gen.add_cards_to_anki(cards, deck, images=images)
        # Clean up any cached video tmpdir for this draft
        anki_gen.cleanup_content(d.get("content"))
        DRAFTS.pop(draft_id, None)
        return jsonify({"added": added, "skipped": skipped, "deck": deck})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/upload_image", methods=["POST"])
def upload_image():
    """Accept a user-pasted/uploaded image and attach it to a draft.

    Returns the new image index so the calling card can reference it via
    `image_ref`, plus a data-URL preview for in-browser rendering.
    """
    draft_id = request.form.get("draft_id")
    if not draft_id or draft_id not in DRAFTS:
        return jsonify({"error": "Draft expired. Please start over."}), 400
    if "file" not in request.files:
        return jsonify({"error": "No image uploaded."}), 400

    f = request.files["file"]
    raw = f.read()
    if not raw:
        return jsonify({"error": "Uploaded image is empty."}), 400

    # Detect mime from magic bytes (clipboard pastes don't always set Content-Type)
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = (f.mimetype or "image/png").lower()
        if not mime.startswith("image/"):
            return jsonify({"error": f"Unsupported file type ({mime})."}), 400

    # Skip the small-image filter — the user explicitly chose this image.
    normalized = anki_gen._normalize_image(raw, mime, min_dim=None)
    if not normalized:
        return jsonify({"error": "Could not decode that image."}), 400

    data, out_mime, ext = normalized
    new_image = {
        "data": data, "mime": out_mime, "ext": ext,
        "caption": "(user-attached)",
        "source_url": "",
        "source_kind": "user",
    }

    d = DRAFTS[draft_id]
    images = d["content"].get("images", [])
    images.append(new_image)
    d["content"]["images"] = images
    new_idx = len(images) - 1

    return jsonify({
        "image_index": new_idx,
        "image": {
            "data_url": _image_data_url(new_image),
            "caption": new_image["caption"],
            "source_kind": new_image["source_kind"],
        },
    })


@app.route("/enrich", methods=["POST"])
def enrich():
    """Actively hunt for a visual that matches one specific card."""
    data = request.get_json(silent=True) or {}
    draft_id = data.get("draft_id")
    card = data.get("card")

    if not draft_id or draft_id not in DRAFTS:
        return jsonify({"error": "Draft expired. Please start over."}), 400
    if not isinstance(card, dict):
        return jsonify({"error": "No card provided."}), 400

    d = DRAFTS[draft_id]
    content = d["content"]
    n_images = len(content.get("images", []))
    card = anki_gen.validate_card(card, n_images=n_images)
    if card is None:
        return jsonify({"error": "Card is malformed."}), 400

    model = anki_gen.resolve_model(data.get("model") or d.get("model"))
    try:
        updated_card, new_image, message = anki_gen.enrich_card_with_visuals(
            content, card,
            source_kind=d.get("source_kind", "web"), model=model,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # If a new image was added to the draft, surface its preview to the browser
    payload = {"card": updated_card, "message": message}
    if new_image is not None:
        payload["new_image"] = {
            "data_url": _image_data_url(new_image),
            "caption": new_image.get("caption", ""),
            "source_kind": new_image.get("source_kind", ""),
        }
        payload["image_index"] = updated_card.get("image_ref")
    elif updated_card.get("image_ref") != card.get("image_ref"):
        # No new image, but we re-assigned an existing one
        payload["image_index"] = updated_card.get("image_ref")
    return jsonify(payload)


def open_browser():
    webbrowser.open("http://localhost:5005")


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable is not set.",
              file=sys.stderr)
        print('Set it with:  export ANTHROPIC_API_KEY="sk-ant-..."',
              file=sys.stderr)
        sys.exit(1)

    print("\n  Anki Generator portal running at http://localhost:5005")
    print("  Press Ctrl+C to stop.\n")
    threading.Timer(1.2, open_browser).start()
    app.run(host="127.0.0.1", port=5005, debug=False)
