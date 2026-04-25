# PropMind: Architecture Decision Record

**Author:** Jordan Park, CTO
**For:** Alex (Co-Founder)
**Date:** 2025-07-14
**Status:** Recommended for adoption

---

Alex — this document answers the five architecture questions we need to lock in before writing a line of production code. Each section gives you a clear recommendation, the trade-offs I considered, and a verdict. Read the verdicts if you're short on time. Read the full sections before you push back.

---

## MVP Architecture in One Paragraph

A property manager connects their Gmail or Outlook inbox through Nylas (one OAuth flow covers both). Nylas sends a webhook to our FastAPI backend every time a new email arrives. A Celery worker picks up that job asynchronously, sends the email content to Claude API, which categorises it (maintenance request, rent enquiry, noise complaint, general) and generates a draft reply. The result is stored in PostgreSQL (hosted on Supabase) and surfaced in a lightweight HTMX dashboard. The property manager reviews the draft, edits if needed, and clicks send — which fires the send command back through Nylas. The whole backend runs on Railway for $5/month. The frontend is static HTML served free from Cloudflare Pages. Total infrastructure cost at MVP: **$5/month**.

---

## Section 1: Email Integration — Nylas vs Gmail API vs Outlook API

**Recommendation: Use Nylas**

The core problem is that we need to support both Gmail and Outlook users. Building direct integrations means two OAuth apps, two token refresh implementations, two webhook schemas, and two maintenance surfaces — forever.

**Why Nylas wins for MVP:**

- **Single OAuth flow** covers both Gmail and Outlook. One integration, one SDK, one set of credentials to manage.
- **Nylas handles token refresh automatically.** OAuth tokens expire. Nylas manages rotation silently. If we go direct, we write that logic ourselves — and when it breaks at 2am, you'll be calling me.
- **Webhook delivery is managed.** Nylas normalises email events across providers and delivers them to our endpoint in a consistent schema. Gmail and Outlook have meaningfully different webhook/push notification systems. Nylas abstracts that entirely.
- **Provider quirks disappear.** Outlook's Graph API and Gmail's API have different rate limits, scopes, and event models. Nylas smooths all of it.

**Trade-offs to understand:**

- Nylas is not free at scale. Pricing is approximately **$0.012 per email event** after the free tier.
- **Free tier: 5 connected accounts.** For a beta with under 20 users, we need to negotiate a startup plan or use multiple workspaces — but the free tier fully covers early validation.
- Direct Gmail/Outlook APIs have **zero marginal API cost** — but the cost is engineering time. Conservatively, building both integrations cleanly would add 3–4 weeks of work and ongoing maintenance. That's the real cost.
- If Nylas raises prices or changes terms, migrating is real work. We accept that risk for now.

**Verdict: Use Nylas for MVP. Revisit at scale if email volume or margin pressure makes the per-event cost meaningful.** The engineering leverage far outweighs the cost at this stage.

> 📎 Reference: [Nylas Email & Calendar API — nylas.com](https://www.nylas.com)

---

## Section 2: Backend Stack

**Recommendation: Python + FastAPI + PostgreSQL + Celery + Redis**

Each component earns its place. Here's the reasoning:

**FastAPI**
- Async-native. Webhook-heavy applications need non-blocking I/O — FastAPI handles concurrent requests without blocking on slow operations.
- Auto-generates OpenAPI documentation from type hints. We get a live API explorer for free, which matters when you're testing integrations.
- Pydantic models give us runtime type validation. Email data from Nylas gets validated on ingestion, not silently corrupted.
- Not Django: Django is a full-stack web framework built for server-rendered apps with ORM, admin, sessions, templating. We don't need 80% of what it ships with, and its sync-first design fights async workloads.
- Not Flask: Flask lacks native async support and type safety. It's fine for prototypes; it's friction for production.

**PostgreSQL via Supabase**
- Our data is relational: users have accounts, accounts have emails, emails have drafts, drafts have statuses. A relational database is the right tool.
- Supabase gives us hosted PostgreSQL with a generous free tier (500MB storage, 2GB bandwidth), built-in auth, and a storage bucket — all without running our own database server.
- Row-level security means we can isolate each property manager's data at the database layer from day one.

**Celery + Redis**
- When Nylas fires a webhook, we need to respond with a 200 OK in under a second — or Nylas retries. But hitting Claude API can take 2–5 seconds.
- Solution: the FastAPI endpoint receives the webhook, drops a job onto a Celery queue (backed by Redis), and returns 200 immediately. A Celery worker picks up the job, calls Claude, and stores the result.
- Redis doubles as the Celery broker and can cache frequently accessed data later.
- Without this pattern, slow Claude responses would cause webhook timeouts and duplicate processing.

**Claude API (Anthropic)**
- Handles two tasks per email: **categorisation** (we define the labels: maintenance request, rent enquiry, noise complaint, general) and **draft response generation**.
- We send a structured prompt with the email subject, body, and sender context. Claude returns structured JSON with category and draft text.
- Claude's instruction-following and long-context handling makes it well-suited for varied, unstructured email content.

**Verdict: FastAPI + Supabase + Celery + Redis. All Python, all open source, deployable in a day. This is the stack.**

---

## Section 3: Frontend Dashboard

**Recommendation: Evolve the existing HTML demo using HTMX + Alpine.js. Do not build a React app.**

**Why not React/Next.js:**
- A React frontend requires a build pipeline: Node.js, npm, webpack or Vite, component architecture, state management, deployment configuration. For a 2-person team shipping an MVP, this is overhead that slows us down without adding user value.
- Next.js adds server-side rendering complexity that we don't need when FastAPI is already our backend.
- We already have `propmail-demo.html` — a working visual shell with the right layout and CSS. Starting from scratch in React throws that away.

**Why HTMX + Alpine.js:**
- **HTMX** lets us make HTML elements trigger server requests and swap content dynamically — without writing JavaScript. A click on "Approve Draft" sends a POST to `/drafts/{id}/approve` and swaps the button state in place. It's server-side rendering that feels interactive.
- **Alpine.js** handles lightweight client-side interactivity: dropdown menus, modal open/close states, toggle animations. It's declared inline in HTML attributes — no component files, no build step.
- The existing demo HTML can be evolved directly. Same CSS classes, same layout structure. We wire HTMX attributes to the real FastAPI endpoints and the demo becomes the product.
- No npm. No webpack. No React developer required.

**When to reconsider:**
- If we hire a React developer and they're frustrated by HTMX, porting is straightforward — the FastAPI endpoints remain identical, only the frontend changes.
- If we build complex client-side state (multi-step workflows, real-time collaboration), React becomes the right call. That's not the MVP.

**Verdict: HTMX + Alpine.js on top of the existing HTML demo. Ship the MVP faster without sacrificing the ability to upgrade later.**

---

## Section 4: Hosting

**Recommendation: Railway for backend, Cloudflare Pages for frontend**

**Railway (backend)**
- $5/month hobby plan. Supports Python, FastAPI, Redis, and Celery workers — all on one platform.
- Deploy by connecting a GitHub repo. Push to main, Railway builds and deploys. No Dockerfile required (though we'll write one anyway for control).
- Managed Redis is available as a Railway add-on — no separate account needed.
- Environment variables managed through their dashboard. Secrets stay out of code.

**Cloudflare Pages (frontend)**
- Free tier. Global CDN. Connect a GitHub repo, set the build command (or none — our frontend is static HTML), and every push deploys globally in under a minute.
- Optional: manage PropMind's domain DNS through Cloudflare too. One dashboard for DNS, SSL, and frontend hosting.

**Supabase (database)**
- Already covered in Section 2. Free tier is sufficient for MVP. No additional hosting decision needed.

**Why not AWS/GCP/Azure:**
- A production-ready AWS setup for this architecture involves: VPC configuration, ECS or EC2 instances, RDS, ElastiCache, IAM roles, load balancers, and CloudWatch logging. That's weeks of DevOps work and ongoing operational complexity.
- For a 2-person founding team, this is the wrong trade. Railway exists precisely to remove this overhead. We can migrate to AWS when we have the scale and the team to justify it.

**Verdict: Railway ($5/month) + Cloudflare Pages (free) + Supabase (free). Total MVP infrastructure cost: $5/month.**

---

## Section 5: Services to Sign Up For

The note above says "Joshua" — I'll flag that the co-founder is Alex, so adjust accordingly. Here are the six services needed, in order of priority:

| Service | URL | Purpose | What to grab after signup |
|---|---|---|---|
| **Nylas** | [nylas.com](https://www.nylas.com) | Email/calendar API — unified Gmail + Outlook integration | `Client ID`, `Client Secret`, `API Key` from the dashboard; note your Callback URI for OAuth setup |
| **Anthropic** | [anthropic.com](https://www.anthropic.com) | Claude API for email categorisation and draft response generation | `API Key` from the console (console.anthropic.com); set spend limits before you add billing |
| **Supabase** | [supabase.com](https://supabase.com) | Hosted PostgreSQL database + auth | `Project URL`, `anon public key`, `service role key` (keep service role secret); database connection string |
| **Railway** | [railway.app](https://railway.app) | Backend hosting — FastAPI app, Celery workers, Redis | No static key; deploy via GitHub OAuth. Note your app's generated public URL for webhook configuration in Nylas |
| **Cloudflare** | [cloudflare.com](https://cloudflare.com) | Frontend hosting via Pages; optional domain DNS management | `Account ID` and `API Token` if using CLI deploys; otherwise deploy via GitHub integration in the Pages dashboard |
| **GitHub** | [github.com](https://github.com) | Source control and CI/CD — connects to Railway and Cloudflare Pages for auto-deploy on push | Create a new private org repo for PropMind; generate a `Personal Access Token` if any service requires programmatic access |

---

## What Jordan Builds First

The following five steps, in order. Nothing else until these are done.

1. **Stand up the skeleton FastAPI app on Railway.** One endpoint: `GET /health` returns `{"status": "ok"}`. Connect the GitHub repo. Confirm auto-deploy works. This proves the deployment pipeline before any real code is written.

2. **Configure Nylas and test the OAuth flow locally.** Register the app in Nylas dashboard, implement the OAuth connection flow, connect one real Gmail account. Confirm Nylas can list emails from that inbox. This is the highest-risk integration — prove it works early.

3. **Wire the Nylas webhook to a FastAPI endpoint + Celery queue.** Nylas sends a `message.created` event; FastAPI receives it, drops it on the Celery queue, returns 200. Celery worker logs the email content to console. No Claude yet — just confirm the async pipeline works end to end.

4. **Integrate Claude API into the Celery worker.** Worker receives email content, sends structured prompt to Claude, receives category + draft JSON, stores result in Supabase. At this point, the backend is functionally complete.

5. **Wire the HTMX dashboard to the real endpoints.** Replace the static demo data in `propmail-demo.html` with HTMX requests to `GET /emails` and `GET /drafts/{id}`. Add the approve/edit/send interactions. This is the first time the full loop — receive email → categorise → generate draft → review in dashboard → send — works in production.

---

*Questions before we start: ask now. Once we're building, changing the stack costs real time.*

— Jordan