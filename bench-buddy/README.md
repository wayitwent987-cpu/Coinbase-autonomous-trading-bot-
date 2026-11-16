# Bench Buddy

A web app for small independent electronics repair shops (phones, computers, consoles).

**What it does**

1. **Customer intake** – a link / QR code. The customer types in their details, device, problem and photos. That creates a ticket.
2. **AI triage** – for every ticket: the 3 most likely causes with rough odds, what to test, what to ask, likely parts, difficulty. Battery, liquid and data-loss hazards are shown in big banners.
3. **Repair guide** – tap a cause and get a step-by-step guide for that exact job, with tick-off steps, tools, parts, and a "double-check these" list.
4. **Quote** – the AI suggests parts and labor hours; the app does the math with *your* rates and markup. You edit and approve before the customer sees anything.
5. **Final price** – when the job's done, start from the quote, enter what you really used, and get the final bill (it tells you if you're over the quote).
6. **Ticket board** – Received → Diagnosing → Waiting on Parts → Ready → Picked Up. Moving a ticket emails the customer.
7. **Customer status page** – every customer gets a private link to check their repair, so they don't need to call.

It also installs like an app: open it on your phone and choose **Add to Home Screen**.

---

## Set it up (about 20 minutes, one time)

You need four free accounts. Do them in this order.

### 1. Supabase (logins, database, photos)

1. Go to [supabase.com](https://supabase.com), create a project. Save the database password somewhere safe.
2. Left menu → **SQL Editor** → **New query**. Open the file `supabase/migrations/20261116000000_init.sql` from this folder, copy *everything*, paste it in, press **Run**. You should see "Success".
3. Left menu → **Project Settings → API**. Keep this tab open; you need three values from it in step 5.
4. Left menu → **Authentication → Sign In / Providers → Email**. For the fastest start, turn **Confirm email** off (you can turn it back on later).

### 2. Anthropic (the AI)

1. Go to [console.anthropic.com](https://console.anthropic.com), add a payment method, and create an **API key**.
2. Optional but recommended: in the Console's privacy/feature settings, turn on **Web search**. The repair guide uses it to look things up. If it's off, guides still work but are marked "not web-checked".

### 3. Resend (emails to customers)

1. Go to [resend.com](https://resend.com) and create an **API key**.
2. For first tests leave the sender as `onboarding@resend.dev`. Resend will then only deliver to **your own** email address, so use that as the "customer" email when testing.
3. Before real customers: add and verify your domain in Resend, then set `EMAIL_FROM` to an address on it (for example `repairs@yourshop.com`).

You can skip Resend at first. Everything works; emails are just logged on the ticket as "Not sent".

### 4. Vercel (puts it on the internet)

1. Go to [vercel.com](https://vercel.com) → **Add New → Project** → pick this GitHub repository.
2. **Important:** set **Root Directory** to `bench-buddy`.
3. Add the **Environment Variables** from step 5 below, then press **Deploy**.
4. When it's live, copy your site address (like `https://bench-buddy-xyz.vercel.app`) and:
   - add it in Vercel as `NEXT_PUBLIC_SITE_URL`, then redeploy;
   - in Supabase → **Authentication → URL Configuration**, set **Site URL** to it and add `https://YOUR-SITE/auth/confirm` under **Redirect URLs**.

### 5. The settings (environment variables)

| Name | Where to find it |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase → Project Settings → API → Project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | same page → "anon" / "publishable" key |
| `SUPABASE_SERVICE_ROLE_KEY` | same page → "service_role" / "secret" key. **Keep this secret.** |
| `ANTHROPIC_API_KEY` | Anthropic Console → API keys |
| `RESEND_API_KEY` | Resend → API keys (optional at first) |
| `EMAIL_FROM` | `onboarding@resend.dev` for testing |
| `NEXT_PUBLIC_SITE_URL` | your Vercel address, no `/` at the end |

To run it on your own computer instead: install [Node.js](https://nodejs.org) 20+, copy `.env.local.example` to `.env.local`, fill it in, then run `npm install` and `npm run dev` and open <http://localhost:3000>.

---

## Test it, one step at a time

1. **Login** – open the site → *Create your shop* → fill in the shop form. You should land on an empty board.
2. **Intake** – *Settings* → scan the QR code with your phone → fill in the form as a pretend customer, with a photo. Write "dropped in water, back is bulging" as the problem. You should see a ticket number and a safety note.
3. **Board** – back on the board the ticket is under *Received* with red/yellow warning badges. Tap *Move to Diagnosing*.
4. **Triage** – open the ticket. Within about 15 seconds you should see 3 causes. The time it took is shown next to *Run triage again*.
5. **Repair guide** – tap *Repair guide* under a cause. Wait about a minute. Tick off a step.
6. **Quote** – tap *Draft quote* under a cause. Fix the part prices (they're marked "AI guess"), then *Approve & email customer*.
7. **Final price** – on the ticket, *Work out the final price*, change something, approve.
8. **Emails** – the bottom of the ticket lists every email and whether it was sent.
9. **Privacy** – make a second account in a private browser window. It must not see the first shop's tickets.

---

## Tuning the AI

Everything the AI is told lives in **one file**: [`src/lib/ai/prompts.ts`](src/lib/ai/prompts.ts).
Edit the text between the backticks, save, and push to GitHub. At the top of that file you can also switch the AI model (for example to a faster, cheaper one) and turn web search on or off.

The words customers receive by email are in [`src/lib/email/templates.ts`](src/lib/email/templates.ts). They are fixed templates, not AI.

## How the safety rules are enforced

- **A keyword check with no AI** ([`src/lib/safety.ts`](src/lib/safety.ts)) flags battery, liquid and data-loss words the moment a ticket is created. It works even if the AI is down, and the AI can never downgrade a "STOP".
- **AI output is only ever shown to the logged-in tech.** Customers only get fixed, human-written wording, which only ever says "stop using it and bring it in".
- **Prices are calculated by code** ([`src/lib/quote-math.ts`](src/lib/quote-math.ts)), never by the AI, and nothing reaches a customer until you approve it.
- **Each shop only sees its own data.** This is enforced inside the database (Row Level Security), not just in the pages.

## Be honest with yourself about the AI

The guides and triage are a fast, well-read assistant, not a service manual. They can be wrong about model-specific details (screw counts, clip positions, parts that need pairing). Each guide lists what it's unsure about: check those before relying on them.

## Not built yet (on purpose)

Payments, inventory, POS, accounting, multiple users or locations, SMS, native mobile apps.

## For developers

```
npm run dev        # run locally
npm test           # unit tests (money math, safety net)
npm run typecheck
npm run lint
npx supabase start # optional: a local Supabase in Docker for testing
```

Layout: pages in `src/app`, server actions in `src/app/actions`, shared logic in `src/lib`, database in `supabase/migrations`.
