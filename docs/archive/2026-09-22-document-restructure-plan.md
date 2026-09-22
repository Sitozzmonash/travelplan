# Documentation Restructure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reorganize TravelPlan documentation so current product, architecture, quality, and operations guidance each has one discoverable home while preserving historical material.

**Architecture:** Move existing current documents into the target taxonomy, consolidate only where their scope overlaps, and preserve superseded designs and reviews under `docs/archive/` or `feedback/done/`. Update repository links after every move; `docs/README.md` becomes navigation only.

**Tech Stack:** Markdown, Git file moves, PowerShell link validation.

---

### Task 1: Establish taxonomy

**Files:** Create `docs/product/`, `docs/architecture/`, `docs/quality/`, `docs/operations/`, `docs/status/`, `docs/archive/`, `feedback/inbox/`, `feedback/done/`.

Create the directories before moving content; do not delete historical records.

### Task 2: Create current-source documents

**Files:** Move/merge product, architecture, quality, and operations material into their designated Markdown files.

Keep one current entry point per topic. Archive design-period files after their current-state content is represented by a target document.

### Task 3: Separate status and feedback

**Files:** Create `docs/status/ROADMAP.md`; move historical development review to `docs/archive/`; organize unresolved feedback in `feedback/inbox/` and completed feedback in `feedback/done/`.

ROADMAP contains only open work; no completed items.

### Task 4: Repair navigation and references

**Files:** Rewrite `docs/README.md`, shorten root `README.md`, and mechanically update Markdown/code references to moved paths.

### Task 5: Verify

**Files:** All Markdown files.

Search for legacy paths, verify local relative links, and inspect `git diff --check`.