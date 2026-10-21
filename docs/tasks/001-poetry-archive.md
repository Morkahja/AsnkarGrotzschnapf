# Development Task 001 — Poetry archive

Status: implemented.

## Goal

Create the local poetry archive and a poem selector. Do not implement image generation, Etsy, or social publishing.

## Archive

SQLite stores, for each poem:

- Title
- Author
- Author's death year, when a citation supports it
- Country or cultural origin, when a source establishes it
- Original language
- Complete poem text
- Original source URL
- Copyright and license information, with verification status
- Import date
- Whether the poem has been used

The importer reads a structured JSON collection. It imports a poem only when the recorded evidence supports commercial use in Sweden.

For this collection, use original poems whose authors died no later than the current Swedish cutoff. In 2026 that cutoff is a death year of 1955. Treat translations as separate works and do not import them here.

The initial collection contains eight complete poems from Project Gutenberg. The poem text is copied exactly, including line breaks.

## Selection

`anskar select` chooses a random eligible poem and prints the title, author, and complete poem.

- One selection stays pending.
- Running select again returns that same poem.
- `anskar select --confirm` marks the pending poem used.
- `anskar select --skip` dismisses the pending poem without marking it used.
- Used poems are not selected again.
- History is stored in SQLite.

## Checks

Run the unit tests. Select a poem from the local database without using the network.
