-- Story Autopilot: public status summary (for the status card embedded in Notion)
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Safe to run again.
--
-- Shows ONLY: each client's name, Live/Paused, whether Meta is connected, how many stories are
-- locked in / have problems this week and next, and when the autopilot last ran.
-- No story names, artwork, times or log messages. Anyone with the status page link can see this.

create or replace function public.autopilot_public_status()
returns json
language sql stable security definer set search_path = public as $$
  select json_build_object(
    'last_run_at', (select last_run_at from autopilot_status where id = 1),
    'ok',          (select ok from autopilot_status where id = 1),
    'clients', coalesce((
      select json_agg(json_build_object(
               'slug', c.slug,
               'name', c.name,
               'paused', c.paused,
               'meta_ok', (l.account->>'ok')::boolean,
               'checked_at', l.checked_at,
               'weeks', l.weeks
             ) order by c.slug)
      from clients c left join lockins l on l.client = c.slug), '[]'::json)
  );
$$;

revoke all on function public.autopilot_public_status() from public;
grant execute on function public.autopilot_public_status() to anon, authenticated;
