# Search by meaning

Keyword search finds mail that contains the words you type. Search by meaning finds mail that is about what you describe, even when it uses other words or another language. Ask for "reviewer comments about construct validity" and it can find a message that says "the second referee doubts that your scale measures what it claims to".

It is optional. It runs entirely on your Mac: the model, the index and the queries never leave it. The only network request is the one-time model download, and it happens only when you run `new-outlook embed --download`.

Contents:

- [Set up](#set-up)
- [Use it](#use-it)
- [How it works](#how-it-works)
- [Choosing a model](#choosing-a-model)
- [Privacy](#privacy)
- [Limits](#limits)
- [Troubleshooting](#troubleshooting)
- [Reference](#reference)

## Set up

1. Install the optional extra. It adds `sentence-transformers`, `sqlite-vec` and `numpy`.

   ```sh
   pipx install 'new-outlook-mcp[semantic] @ git+https://github.com/michalhron/new-outlook-mcp.git'
   # or, on an existing install:
   pipx inject new-outlook-mcp sentence-transformers sqlite-vec numpy
   ```

2. Download the model and embed the archive:

   ```sh
   new-outlook embed --download
   ```

   The first run fetches the model from Hugging Face (about 470 MB for the default) and then embeds every message, newest first. It prints progress and an estimate of the time left. On an Apple Silicon Mac it uses the GPU (Metal) and otherwise the CPU. You can stop it with Ctrl-C at any time. The next `new-outlook embed` continues where it stopped.

3. Check the state:

   ```sh
   new-outlook embed --status
   ```

From then on, every sync embeds the new mail it imported. That includes syncs by the watcher, the LaunchAgent and the `sync_now` tool. No further setup is needed.

## Use it

From Claude, through three tools:

| Tool | Use it for |
|---|---|
| `semantic_search` | A description of what you are looking for, in your own words. Takes the filters `sender`, `recipient`, `folder`, `account`, `date_from`, `date_to` and `has_attachment`. `mode` is `hybrid` (default) or `semantic`. |
| `search_emails` with `mode: "hybrid"` or `"semantic"` | The same search with the full filter set of `search_emails`, plus `attachment_name`, `sort` and paging with `offset`. |
| `find_similar` | Mail that resembles a given email (`email_id`) or a given attachment (`attachment_id`). |

From the terminal:

```sh
new-outlook search --mode hybrid "reviewer comments about construct validity"
new-outlook search --mode semantic "schůzka o rozpočtu" --date-from 2025-01-01
new-outlook search --mode hybrid "travel reimbursement" --sender finance --json
```

Each result carries the usual message summary plus:

- `score`: cosine similarity in `semantic` mode, the fused score in `hybrid` mode (see below). Compare scores only within one result list.
- `matched`: `semantic`, `keyword` or `both`, telling which ranking found the message.
- `match`: which part matched best, `subject`, `body` or `attachment`. For an attachment it also gives `attachment_id` and `filename`.
- `snippet`: the best matching passage.

Which mode to pick:

- `keyword` (the default of `search_emails`) when you know exact words, names, codes or numbers.
- `hybrid` (the default of `semantic_search`) for most questions. It finds both exact hits and paraphrases.
- `semantic` when the words you remember are probably wrong, or when you search across languages.

If search by meaning is not set up, `search_emails` in `hybrid` mode falls back to keyword results with a note saying why. `semantic` mode returns an error that says what to run.

## How it works

Search by meaning has two halves. Indexing turns every message into vectors once. Querying turns your question into a vector and looks for the nearest ones.

```
 indexing (embed, and after each sync)
 message ─► subject ─────────────────────────────────┐
         ─► body ─► strip quotes and signature ─► chunks ─► model ("passage: ") ─► vectors ─► chunk_vec
         ─► attachment text (local files) ─► chunks ─┘

 querying
 question ─► model ("query: ") ─► vector ─► nearest chunks ─► best chunk per message ─► filters + privacy ─┐
          ─► keyword query (OR of words) ─► FTS5 bm25 ─► filters + privacy ─────────────────────────────────┴─► RRF ─► results
```

### 1. Chunking

A whole message is too long and too mixed for one vector. Each message is split into chunks first (`chunking.py`, `semantic.build_chunks`):

- The subject is a chunk of its own, so a short message can still be found by its subject.
- The body is cleaned and then split. Cleaning removes:
  - quoted history below "On ... wrote:", "-----Original Message-----" and Outlook reply headers (From, Sent, To, Subject),
  - lines starting with `>`, while the answers between them stay,
  - signatures after `-- `, after a short closing such as "Best regards" with a few short lines below it, and device lines such as "Sent from my iPhone".

  The patterns cover English, Czech, German, French, Spanish, Italian, Danish, Dutch and Finnish. A reply is then embedded for what its author wrote and not for the thread below it. If cleaning would leave nothing, the text is kept as it was.
- Paragraphs are packed into chunks of about 700 characters and at most 1,000. A paragraph longer than that is cut at a line break, a sentence end, a comma or a space. When the last paragraph of a chunk is short (up to 200 characters), it is repeated at the start of the next chunk, so a thought that spans two chunks is not lost.
- Attachment text is chunked the same way without cleaning. Each attachment chunk is embedded with the file name in front of it, so "the budget spreadsheet" can match a file called `budget-2025.xlsx`. Only attachments whose file is on this Mac are read, and only the first 60,000 characters or 60 chunks of each. Small inline images such as signature logos are skipped.

The chunks are stored in the `chunks` table of `archive.db` with their offsets into the original text.

### 2. Embedding

The model turns each chunk into a vector of numbers (384 for the default model) so that texts with similar meaning get nearby vectors. The default model, `intfloat/multilingual-e5-small`, was trained on about 100 languages, and texts with the same meaning land near each other whatever their language. That is why an English question finds a Czech message.

The e5 models expect a prefix: chunks are embedded as `passage: <text>` and questions as `query: <text>`. The code adds these. Vectors are normalized to length 1.

The model name is stored in the archive. Vectors from different models cannot be compared, so the tools refuse to mix them. To switch models you re-embed everything (see [Choosing a model](#choosing-a-model)).

### 3. Storage

Vectors live in `archive.db` next to the mail. With the `sqlite-vec` extension they go into a `chunk_vec` virtual table with cosine distance. If the extension cannot be loaded in your Python, they go into a plain table of float32 blobs (`chunk_vectors`) and the search runs in numpy. Both give the same results. The blob store is slower on large archives.

Indexing goes message by message, newest first, in batches of 32 messages. Each batch is one transaction. The `embedded_messages` table records which messages are done, which is what makes `embed` resumable.

### 4. Retrieval

For a question, the server:

1. Embeds the question with the same model.
2. Finds the nearest chunks by cosine similarity and keeps the best chunk per message. The best chunk decides the message's rank and supplies its snippet.
3. Applies your filters and the privacy rules. The nearest-neighbour search runs first and the filters after it. When filters are set, it looks deeper (up to 4,096 chunks with `sqlite-vec`, all chunks with the blob store) so that enough results survive the filters.
4. In `hybrid` mode, runs a keyword search in parallel. A plain question becomes an OR of its words longer than two characters (at most 16), so one missing word does not empty the result. If you use FTS5 syntax yourself (quotes, `AND`, `OR`, `NOT`, `*`), your query is used as written. Keyword hits are ranked by bm25 with the subject weighted highest.
5. Fuses the two rankings with Reciprocal Rank Fusion.

Both rankings are cut to a pool of at least 100 messages, or five times the page you asked for, whichever is larger.

### 5. Reciprocal Rank Fusion

Cosine similarities and bm25 scores are on different scales, so they cannot be added. Reciprocal Rank Fusion uses only the ranks:

```
score(message) = Σ over rankings  1 / (60 + rank)
```

Ranks start at 1. A message ranked first by meaning and third by keywords scores 1/61 + 1/63 ≈ 0.0323. A message found by only one ranking, at rank 1, scores 1/61 ≈ 0.0164. So a message both rankings agree on rises to the top, and a strong hit from either side still appears. The constant 60 is the value from the original paper (Cormack, Clarke and Büttcher, 2009). It damps the difference between rank 1 and rank 2 so that no single list dominates. Ties keep the order in which the messages were first seen.

### 6. Finding similar mail

`find_similar` takes the stored vectors of the email (or of one attachment), averages them into one vector and searches with it as in step 2. The source message itself is left out. The item must already be embedded.

### 7. Keeping the index current

After each sync, the importer embeds up to 2,000 new messages. The rest wait for the next sync or for `new-outlook embed`. This happens only after you have run `embed` once, and a sync never downloads a model.

## Choosing a model

| Model | Download | Dimensions | Notes |
|---|---|---|---|
| `intfloat/multilingual-e5-small` | about 470 MB | 384 | Default. Fast, about 100 languages. |
| `intfloat/multilingual-e5-base` | about 1.1 GB | 768 | Larger and slower than small. Usually ranks better. |
| `BAAI/bge-m3` | about 2.3 GB | 1024 | Largest and slowest of the three. No prefixes. |

Other sentence-transformers models also load. Models outside this table get no prefixes.

To switch:

```sh
new-outlook embed --download --reembed --model intfloat/multilingual-e5-base
```

`--reembed` deletes all chunks and vectors first. Without it, `embed` refuses to mix models.

## Privacy

- Nothing leaves the Mac. The model runs locally. After the download, the model loads with `local_files_only`, so it never contacts Hugging Face again.
- Privacy scopes apply to every result. Excluded mail is dropped at import, so it is never chunked or embedded. A rule added later hides matching mail from semantic, hybrid and `find_similar` results at once, and `find_similar` refuses a hidden email as its source.
- `new-outlook purge-excluded` deletes the chunks and the vectors of purged messages together with the messages, and then compacts the database.
- Chunk text is a copy of mail text and lives in `archive.db`. Protect that file like the rest of the archive. See [privacy.md](privacy.md).

## Limits

- There is no similarity cutoff. A search always returns the nearest messages, even when none is a good match. Look at `matched` and at the snippets. A result list where nothing matched by keyword and the snippets look unrelated means the archive probably has nothing on the topic.
- A message is embedded once. If a later sync fills in a body that was missing or caches an attachment file that was not on disk, the message keeps its old chunks. Run `new-outlook embed --reembed` now and then if this matters to you.
- Orphan files (cached files whose message is gone, see `search_files`) are searchable by keyword only.
- Filters run after the nearest-neighbour search. With a very narrow filter on a very large archive, a relevant message that ranks below the deepest search level can be missed. Narrow the question or use keyword search with the same filter.
- Short questions of one or two words work better as keyword searches.
- The first full embed takes time, more with many attachments. `--limit` embeds part of the archive, newest first, so recent mail is searchable early.

## Troubleshooting

| Message or symptom | What to do |
|---|---|
| "Search by meaning is not installed" | Install it as in [Set up](#set-up). |
| "The model ... is not on this Mac yet" | Run `new-outlook embed --download` once. |
| "No embeddings yet" | Run `new-outlook embed --download` once. |
| "This archive is embedded with X. Use --reembed to switch to Y." | You asked for another model. Re-embed or drop `--model`. |
| "the sqlite-vec extension cannot be loaded" | Your Python's SQLite does not allow extensions. Run `new-outlook embed --reembed` to rebuild with the blob store, or install a Python whose `sqlite3` module allows extensions. |
| "this item has no embeddings yet" from `find_similar` | Run `new-outlook embed`. |
| New mail does not show up in semantic results | Check `new-outlook embed --status`. If many messages are pending, run `new-outlook embed`. |

## Reference

Commands:

```
new-outlook embed [--download] [--model NAME] [--limit N] [--batch N] [--reembed] [--status]
new-outlook search QUERY [--mode keyword|semantic|hybrid] [--sender S] [--folder F]
                         [--date-from D] [--date-to D] [--limit N] [--json]
```

Settings:

| Setting | Value |
|---|---|
| Chunk size | about 700 characters, at most 1,000 |
| Overlap | the previous chunk's last paragraph when it is 200 characters or shorter |
| Attachment cap | 60,000 characters and 60 chunks per attachment |
| RRF constant | 60 |
| Candidate pool | max(100, 5 × (offset + limit)) per ranking |
| Embedded after each sync | up to 2,000 messages |
| Batch | 32 messages per transaction |

Environment variables:

| Variable | Effect |
|---|---|
| `NEW_OUTLOOK_VECTOR_STORE` | `vec0` or `blob` forces a vector store. Default: `vec0` when `sqlite-vec` loads. |
| `NEW_OUTLOOK_EMBEDDER` | `fake` uses a deterministic word-hashing embedder. For tests only. |

Code: `chunking.py` (cleaning and chunking), `embedder.py` (models), `vectors.py` (vector store), `semantic.py` (indexing, retrieval, fusion). Tests: `tests/test_chunking.py`, `tests/test_semantic.py`.
