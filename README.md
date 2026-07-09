# Foundry Agent + Google Drive (via MCP)

A minimal, working example of a **Microsoft Foundry agent** (Azure AI Foundry
Agent Service) that connects to your **personal Google Drive** through the
official **Google Drive remote MCP server**, so you can ask questions in natural
language about your own documents:

> _"What does my Q3 marketing plan say about budget?"_
> _"Summarize the file called Onboarding Checklist."_
> _"List my most recently modified spreadsheets."_

Built with the new **Microsoft Foundry SDK** — [`azure-ai-projects`](https://pypi.org/project/azure-ai-projects/)
2.x *prompt agents* plus the OpenAI-compatible **Responses API** — and the
[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tool.

---

## Architecture

```mermaid
flowchart LR
    U([You]) -- question --> CLI[main.py CLI]
    CLI -- create agent + run --> FA[Foundry Agent Service]
    FA -- MCP tool call\n(Authorization: Bearer &lt;google token&gt;) --> GD[Google Drive MCP server\ndrivemcp.googleapis.com]
    GD -- reads --> DRIVE[(Your Google Drive)]
    CLI -. OAuth 2.0 consent .-> GOOG[Google OAuth]
    GOOG -. access + refresh token .-> CLI
```

1. `main.py` runs a local **Google OAuth 2.0** flow and caches an access/refresh
   token (`token.json`).
2. It publishes a Foundry **prompt agent** (`agents.create_version`) whose only
   tool is the **Google Drive MCP server**.
3. On every turn, the app injects a **fresh Google access token** as the
   MCP tool's `authorization` field — the token the Drive MCP server expects.
4. It chats through the OpenAI-compatible **Responses API** over a
   `conversation`, so multi-turn context is kept. The model decides when to call
   Drive tools (`search_files`, `read_file_content`, …); the service runs them
   and returns the answer.

### How authentication really works (important)

Google offers the Drive MCP server as a **remote OAuth-protected endpoint**.
"First‑class" clients like Antigravity and Claude perform the interactive OAuth
handshake for you. **Foundry's MCP tool doesn't run that interactive handshake** —
your application must obtain the token. So this project takes responsibility for
OAuth itself: it runs the consent flow locally, then passes the resulting access
token to Foundry in the MCP tool's dedicated **`authorization`** field. (Foundry
**rejects** tokens placed in `headers` — you get
`Headers that can include sensitive information are not allowed`.) Google access
tokens expire (~1 hour), so the app refreshes
the token before every turn using the cached refresh token.

> This is a **demo / developer** pattern. For production you'd centralize token
> lifecycle in a [Foundry Toolbox](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox)
> or a small MCP proxy rather than handling raw Google tokens in the client. See
> [Limitations](#limitations--caveats).

---

## Prerequisites

- **Python 3.10+**
- An **Azure subscription** with a **Microsoft Foundry project** and a deployed
  chat model (e.g. `gpt-4o`).
- **Azure CLI** (`az`) for local auth (`DefaultAzureCredential`).
- A **Google account** with some files in Google Drive.
- A **Google Cloud project** where you can enable APIs (the Google Drive MCP
  server is in **Developer Preview**).

---

## Part 1 — Google Cloud setup

You need (a) the APIs enabled, (b) an OAuth consent screen, and (c) an OAuth
client whose JSON you download as `credentials.json`.

### 1.1 Enable the required APIs

In your Google Cloud project, enable both:

```bash
gcloud services enable drive.googleapis.com    --project=PROJECT_ID
gcloud services enable drivemcp.googleapis.com --project=PROJECT_ID
```

(Or use the Console: **APIs & Services > Enable APIs and services**.)

### 1.2 Configure the OAuth consent screen

**Google Auth Platform > Branding** → set an app name (e.g. `Drive MCP Demo`)
and support email. Under **Audience**, pick **Internal** if available, otherwise
**External** and add your own email as a **Test user**.

Under **Data Access > Add or Remove Scopes**, add:

- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/drive.file`

### 1.3 Create an OAuth client and download `credentials.json`

**Google Auth Platform > Clients > Create Client**:

- **Application type: Desktop app** ← simplest for this local example.
- Create it, then **Download JSON** and save it in the project root as
  `credentials.json`.

> A **Desktop app** client works with the local loopback flow used here, so you
> don't need to register any redirect URI. (If your org restricts the MCP
> endpoint to specific web clients, see [Troubleshooting](#troubleshooting).)

---

## Part 2 — Microsoft Foundry setup

1. In the [Foundry portal](https://ai.azure.com), open (or create) a project and
   **deploy a model** (e.g. `gpt-4o`). Note the deployment **Name**.
2. From the project **Overview**, copy the **Project endpoint**
   (looks like `https://<resource>.services.ai.azure.com/api/projects/<project>`).
3. Make sure your identity has the **Azure AI User** (or Contributor/Owner) role
   on the project.
4. Sign in locally so `DefaultAzureCredential` can get a token:

   ```bash
   az login
   ```

---

## Part 3 — Install & configure

```bash
git clone <this-repo-url>
cd foundry-gdrive-mcp-agent

python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

Copy the env template and fill it in:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Set at least `PROJECT_ENDPOINT` and `MODEL_DEPLOYMENT_NAME`. Put your downloaded
`credentials.json` in the project root.

---

## Part 4 — Run it

**1. Authenticate to Google** (opens a browser once, caches `token.json`):

```bash
python main.py auth
```

**2. (Optional) Verify Drive access** directly, before involving the agent:

```bash
python check_google.py
```

You should see `HTTP 200` and a few of your file names.

**3. Ask a question:**

```bash
python main.py ask "Summarize my most recent document"
```

**4. Or start an interactive chat** (keeps conversation memory in one thread):

```bash
python main.py chat
```

```
you> what files did I edit most recently?
agent> Created agent version: gdrive-mcp-agent (v1)
agent> Your three most recently modified files are …
```

---

## Available Google Drive MCP tools

The agent can call any of these (unless you restrict them with
`MCP_ALLOWED_TOOLS`):

| Tool | Purpose |
| --- | --- |
| `search_files` | Find files by name/content |
| `read_file_content` | Read a file's contents |
| `get_file_metadata` | File metadata (type, dates, owner) |
| `get_file_permissions` | Who has access |
| `list_recent_files` | Recently modified files |
| `download_file_content` | Download raw content |
| `create_file` | Create a new file |
| `copy_file` | Copy an existing file |

For read-only Q&A, a good allow-list is
`search_files,read_file_content,get_file_metadata,list_recent_files`.

---

## Configuration reference

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `PROJECT_ENDPOINT` | ✅ | — | Foundry project endpoint |
| `MODEL_DEPLOYMENT_NAME` | ✅ | — | Deployed model name |
| `MCP_SERVER_URL` | | `https://drivemcp.googleapis.com/mcp/v1` | Drive MCP endpoint |
| `MCP_SERVER_LABEL` | | `google_drive` | Label for the tool |
| `MCP_ALLOWED_TOOLS` | | _(all)_ | Comma-separated allow-list |
| `MCP_REQUIRE_APPROVAL` | | `never` | `never` (service runs tools) or `always` (app auto-approves + logs) |
| `GOOGLE_OAUTH_CLIENT_SECRETS` | | `credentials.json` | OAuth client JSON path |
| `GOOGLE_TOKEN_PATH` | | `token.json` | Cached token path |
| `GOOGLE_OAUTH_PORT` | | `0` | Local OAuth callback port |
| `AGENT_NAME` | | `gdrive-mcp-agent` | Agent display name |

---

## Project layout

```
foundry-gdrive-mcp-agent/
├── main.py               # CLI: auth / ask / chat
├── check_google.py       # Diagnostic: verify Drive token directly
├── requirements.txt
├── .env.example
└── src/
    ├── config.py         # Settings from environment
    ├── google_auth.py    # Google OAuth 2.0 (obtain/refresh token)
    └── foundry_agent.py  # Foundry agent + MCP tool + approval loop
```

---

## Troubleshooting

- **`DefaultAzureCredential` fails** → run `az login`; confirm your role on the
  Foundry project and that `PROJECT_ENDPOINT` is correct.
- **`credentials.json not found`** → download the OAuth client JSON from Google
  Cloud Console and place it in the project root (or set
  `GOOGLE_OAUTH_CLIENT_SECRETS`).
- **`403 / access_denied` from Google** → the Drive/DriveMCP APIs aren't enabled,
  the scopes weren't granted, or (for an **External** consent screen) your email
  isn't added as a **Test user**.
- **MCP call returns 401** after a while → the Google token expired. This app
  refreshes per turn; if you customized the flow, re-run `python main.py auth`.
- **MCP endpoint rejects a Desktop-app token** → some org policies only allow
  specific **Web application** OAuth clients for the MCP endpoint. Create a Web
  application client instead, add an authorized redirect URI such as
  `http://localhost:PORT/` (matching `GOOGLE_OAUTH_PORT`), and set
  `GOOGLE_OAUTH_PORT` to that fixed port.
- **Package/import errors** → this project uses the **new Foundry projects API**
  (`azure-ai-projects>=2.3.0`, which pulls in `openai`). It is **not** compatible
  with `azure-ai-projects` 1.x. Reinstall into a clean venv with
  `pip install -r requirements.txt`.
- **`invalid_payload` / "Headers that can include sensitive information are not
  allowed in the headers property for MCP tools. Use project_connection_id
  instead."** → an older build put the Google token in the MCP tool `headers`.
  The current code sends it in the dedicated MCP tool `authorization` field
  instead, which Foundry allows. `git pull` and reinstall (`pip install -r
  requirements.txt`). Advanced: to store the credential server-side instead, set
  `MCP_PROJECT_CONNECTION_ID` to a Foundry connection you create in the portal.
- **`server_error` / "Sorry, something went wrong" on a run** → almost always the
  MCP call failing server-side: an expired/invalid Google token, the Drive/DriveMCP
  APIs not enabled, or the OAuth client type being rejected (see the Desktop-app
  note above). Run `python check_google.py` to confirm the token still works.

---

## Security & limitations

### Security
- `credentials.json`, `token.json` and `.env` are git-ignored — **never commit
  them**.
- The token grants read access to your Drive. Treat it like a password.
- **Indirect prompt injection:** documents can contain hidden instructions. Only
  point the agent at Drives you trust, keep `MCP_REQUIRE_APPROVAL=always` while
  experimenting, and review tool calls. See Google's
  [security guidance](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server).

### Limitations / caveats
- The Google Drive MCP server is a **Developer Preview**; endpoint, scopes and
  behavior may change.
- Foundry doesn't manage the Google OAuth lifecycle, so **this client does**.
  That's fine for demos but not ideal for production — prefer a **Foundry
  Toolbox** or a dedicated MCP proxy that centralizes credential handling and
  token refresh.
- Third-party MCP servers are not tested or verified by Microsoft; you are
  responsible for the data you share with them.

---

## References

- [Quickstart: Create a prompt agent (new Foundry SDK)](https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent?tabs=python)
- [Connect Foundry agents to MCP servers](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol)
- [Configure the Google Drive MCP server](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server)
- [azure-ai-projects MCP samples](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/agents/tools)

## License

[MIT](LICENSE)
