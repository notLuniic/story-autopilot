-- Story Autopilot: start the GitHub workflow every 15 minutes.
-- GitHub's own schedule is best-effort and skips most runs on quiet free repos; a "run now"
-- request starts within seconds. GitHub's schedule stays on as a backup (an extra run never
-- double-posts).
--
-- Before running: make a fine-grained GitHub token (Only select repositories -> story-autopilot,
-- Permissions -> Actions: Read and write, nothing else).
--
-- NEVER put the token in this file in GitHub (the repo is public). Copy this script into
-- Supabase -> SQL Editor, replace PASTE_TOKEN_HERE there, and run it. The token is stored
-- encrypted in Supabase Vault; the timer reads it from there. Afterwards, delete the query from
-- the SQL Editor (or set it back to PASTE_TOKEN_HERE) so no plain copy is left.
-- Change the repo path below if your GitHub account or repo name differ.

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

select vault.create_secret('PASTE_TOKEN_HERE', 'github_dispatch_token',
  'Fine-grained token: Actions read/write on story-autopilot only');

select cron.schedule(
  'story-autopilot',
  '*/15 * * * *',
  $$
  select net.http_post(
    url := 'https://api.github.com/repos/notLuniic/story-autopilot/actions/workflows/post-stories.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'github_dispatch_token'),
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'story-autopilot'),
    body := '{"ref":"main","inputs":{"command":"run"}}'::jsonb
  );
  $$
);

-- Check it's working (204 = GitHub accepted the request):
--   select created, status_code, content from net._http_response order by created desc limit 5;
-- Renew the token (when it expires):
--   select vault.update_secret((select id from vault.secrets where name = 'github_dispatch_token'), 'NEW_TOKEN');
-- Remove the timer:
--   select cron.unschedule('story-autopilot');
