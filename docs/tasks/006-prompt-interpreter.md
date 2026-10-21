# Development Task 006 — Prompt interpreter

Status: implemented.

## Goal

The prompt lab can ask the local Ollama model `qwen3:8b` to turn a source text into one visual prompt. Direct generation still sends the selected text through the house style. Production generation does not use the interpreter.

## Interpretation

The system instruction tells the model to read the whole text, form one visual concept, follow the selected house style, and answer in about 80 to 150 words. It must not quote the source or ask for lettering. Painterly is the default preset. Dreamlike, Symbolic, and a custom style prompt are sent with the source when house style is on.

The source is not shortened to fit. If the estimated size, or Ollama's own token count, exceeds the model's context window, the request stops with an error. The chat request sets `truncate` to false and `think` to false. `qwen3:8b` is unloaded after the interpretation and again before image generation when it is still loaded.

## Lab

Direct and Interpreted are separate choices. Interpret with Ollama fills an editable visual prompt and selects Interpreted. Generating in that mode sends the visual prompt by itself, including any edit, and keeps the negative prompt. The original source is stored beside the interpretation.

## Metadata

A new interpreted run records the source text and its hash, the Ollama prompt and its hash, the edited visual prompt, whether it was edited, the house style, the Ollama model and settings, the image model and sampler settings, the negative prompt, and the interpretation and image timestamps.
