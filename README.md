# Story Autopilot

Schedules Instagram and Facebook Page **stories** (images and videos) for free. You upload and schedule in a web app; the autopilot posts each story at its time through Meta's own API.

Built by Oliver for use across clients.

## How it works

| Part | Where it runs | What it does |
|---|---|---|
| **The app** (`docs/`) | GitHub Pages | Sign in, upload a story, choose Instagram / Facebook / both, pick a day and time (once, or every week). Shows this week and next week, the Monday lock-in and the error log. |
| **Storage and database** | Supabase (free plan) | Keeps the media in a **private** bucket and the schedule, posting records and error log in a database. Only your login can read or change anything. |
| **The autopilot** (`autopilot.py`) | GitHub Actions, every 15 minutes | Posts whatever is due, retries failures for up to 3 hours, runs the lock-in, logs problems, tidies old media. |

**Monday lock-in.** On the first run each Monday, and within 15 minutes of any change, every story for this week and next week is checked: media present and in a format Meta accepts, accounts reachable, token valid. Each story shows as **Scheduled** or **Problem**; problems go to the error log.

**Privacy.** Media is never public. When a story is due, the autopilot gives Meta a link to that one file that stops working after an hour. The GitHub repo holds only code plus `docs/config.js`, which contains your Supabase address and *publishable* key (both made to be public: the database refuses anyone who isn't signed in with an approved login). The secret keys live in GitHub Secrets.

**Business Suite.** Meta's API can't put a story into the Business Suite planner as scheduled; it can only publish one immediately. Use the app's weekly view to confirm the week. Each story shows up in Business Suite once it has gone out.

Stories posted through the API can't have stickers (polls, links, location, mentions), music, or text added in Instagram. Put any text in the artwork.

## Setup

### 1. Supabase (15 minutes)

1. Sign up at supabase.com (free) and create a project. Region: **Sydney**. Save the database password somewhere safe; you won't need it day to day.
2. **Authentication → Sign In / Providers:** turn **off** "Allow new users to sign up".
3. **Authentication → Users → Add user → Create new user:** your email and a strong password, with "Auto Confirm User" ticked. This is the login for the app.
4. **SQL Editor → New query:** paste all of `supabase/setup.sql` and click **Run**. It creates the tables, the private `stories` bucket, the access rules and the GWC client, and gives the login you just made access. Run it **after** step 3.
5. **Project Settings → API Keys** (and **Data API** for the URL), note:
   - the **Project URL** (`https://xxxx.supabase.co`)
   - the **publishable key** (`sb_publishable_…`, or the older `anon` key)
   - the **secret key** (`sb_secret_…`, or the older `service_role` key). This one gives full access, so keep it only in GitHub Secrets.

### 2. GitHub (15 minutes)

1. Create a **public** repository called `story-autopilot` on your own account (public keeps Actions free; no client data goes in it).
2. Upload everything in this folder, keeping the folder structure. Check `.github/workflows/` has `post-stories.yml` and `publish-app.yml`; if the browser skipped the hidden folder, add them with **Add file → Create new file**.
3. Edit `docs/config.js` on GitHub: fill in the Project URL and the **publishable** key. Commit.
4. **Settings → Pages → Source: GitHub Actions.** Then **Actions → Publish app → Run workflow.** The app is at `https://<your-username>.github.io/story-autopilot/`. Sign in with the login from Supabase step 3.
5. **Settings → Secrets and variables → Actions → New repository secret:** `SUPABASE_URL` (the Project URL) and `SUPABASE_SERVICE_KEY` (the secret key).

### 3. Meta, per client (about an hour)

Done inside the **client's** business portfolio, so the client keeps ownership and can remove access whenever they like. You need admin access to the portfolio.

1. **Check the accounts.** Instagram must be a **Business** account linked to the Facebook Page, and both must be in the client's portfolio (business.facebook.com → Settings → Accounts).
2. **Create the Meta app.** developers.facebook.com → My Apps → Create App → the use case for managing content on Instagram (or Other → Business). Connect it to the client's portfolio.
3. **System user and token.** business.facebook.com → Settings → Users → System users → Add (`story-autopilot`, role Admin) → Assign assets: the app, the Page and the Instagram account, full control → **Generate new token** → the app, expiry **Never**, permissions: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `business_management`. Copy it, as it's only shown once.
4. **Secrets:** `GWC_META_TOKEN` = the token, and `META_TOKEN` = the same token (temporary).
5. **IDs:** Actions → Post stories → Run workflow → `ids`. Add the printed Page ID and Instagram ID as `GWC_PAGE_ID` and `GWC_IG_ID`. Delete `META_TOKEN`.
6. **Check:** Run workflow → `check`. It lists the Instagram username, Page name and "never expires", and the app's lock-in panel shows **Meta connected**.
7. **Test one story:** schedule a story in the app, open it, copy its Story ID, then Run workflow → `post` with that ID. It posts straight away. Delete it from Instagram/Facebook afterwards if you don't want it up.
8. **Go live:** in the app, Settings → **Go live**. Until then nothing posts on schedule (the lock-in still runs).

If Meta says the app must be **Live** for posts to be public, switch it to Live in the app dashboard (it asks for a privacy policy URL).

Secret names always start with the client's code in capitals: `GWC_…` for `gwc`.

## Limits

- Image: JPG (the app converts PNG/WebP), up to 8 MB, 1080×1920 best.
- Video: MP4 or MOV, 3–60 seconds, **up to 50 MB** (Supabase free plan), 9:16, H.264.
- Storage: 1 GB on the free plan. The autopilot deletes media for stories more than 8 days past their last date, so it doesn't fill up.
- Supabase pauses free projects after a week with no activity. The autopilot connects every 15 minutes, which keeps it active; if it's ever paused, restore it from the Supabase dashboard.
- GitHub runs scheduled jobs on a best-effort basis: stories usually go out 4–10 minutes after their time, sometimes later. After 3 hours a story is logged as missed.
- GitHub switches off schedules in repos with no activity for 60 days; the workflow re-confirms its schedule daily. If the app's top bar ever says the autopilot last ran hours ago, check the Actions tab and click **Enable workflow** if needed.
- Meta API version: `GRAPH_VERSION` (default `v25.0`). If posts start failing with a version error, bump it.

## Adding a client

1. In Supabase's SQL Editor:
   `insert into clients (slug, name, timezone, platforms) values ('acme', 'Acme Bar', 'Australia/Sydney', '{instagram,facebook}');`
   (Instagram only: `'{instagram}'`.)
2. Do the Meta setup in their portfolio and add `ACME_META_TOKEN`, `ACME_IG_ID`, `ACME_PAGE_ID`.
3. Pick them from the client menu in the app; switch them live in Settings after a test.

## Testing locally

`python tests/test_engine.py` runs the autopilot against a stand-in Supabase (`tests/fake_supabase.py`) and a fake Meta that downloads each signed link the way Meta would: posting, no doubles, retries, missed posts, the lock-in, tidying, video checks, and the Graph API call sequence.
