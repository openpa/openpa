# OPENPA Agent

Personal AI Assistant Agent

## Setup

1. Install dependencies:

   ```bash
   uv sync
   ```

2. Create a `.env` file with your OpenAI API key:
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and add your actual OpenAI API key.

## DBeaver SQLite Configuration

In DBeaver, foreign key enforcement is off by default for SQLite. To enable cascade deletes:

1. Right-click your SQLite connection > **Edit Connection**
2. Go to **Connection Settings** > **Initialization**
3. Add `PRAGMA foreign_keys=ON;` to the **Bootstrap queries** (or "Keep-Alive" section depending on your DBeaver version)
4. Reconnect
