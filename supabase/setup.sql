-- Story Autopilot: Supabase setup
-- Run once in your Supabase project: SQL Editor -> New query -> paste all of this -> Run.
-- Before running it, create your own login (Authentication -> Users -> Add user), because
-- the last step gives every login that exists at that moment access to the app.
-- Safe to run again: it only adds what's missing.

-- ---------------------------------------------------------------- who may use the app
create table if not exists public.app_users (
  user_id uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

create or replace function public.is_app_user() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.app_users where user_id = auth.uid());
$$;

-- ---------------------------------------------------------------------------- tables
create table if not exists public.clients (
  slug text primary key check (slug ~ '^[a-z0-9-]+$'),
  name text not null,
  timezone text not null default 'Australia/Sydney',
  platforms text[] not null default '{instagram,facebook}'
    check (cardinality(platforms) > 0 and platforms <@ array['instagram','facebook']),
  paused boolean not null default true,          -- nothing posts until switched live in the app
  grace_hours numeric not null default 3,
  schedule_changed_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create table if not exists public.posts (
  id text primary key,
  client text not null references public.clients(slug) on delete cascade,
  name text not null default '',
  media_path text not null,
  poster_path text,
  type text not null check (type in ('image','video')),
  platforms text[] not null check (cardinality(platforms) > 0 and platforms <@ array['instagram','facebook']),
  repeat text not null default 'once' check (repeat in ('once','weekly')),
  date date not null,                            -- the only date, or the first date of a weekly story
  time text not null check (time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
  until date,                                    -- weekly stories stop after this date
  paused boolean not null default false,
  width int, height int, duration numeric, size bigint,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists posts_client_idx on public.posts(client);

-- one row per story per date: what was posted where (written by the autopilot)
create table if not exists public.occurrences (
  key text primary key,                          -- "<post id>@<date>"
  client text not null references public.clients(slug) on delete cascade,
  post_id text not null,
  name text, date date not null, time text not null,
  media_path text, poster_path text, type text,
  platforms jsonb not null default '{}',
  updated_at timestamptz not null default now()
);
create index if not exists occurrences_client_date_idx on public.occurrences(client, date);

-- the latest Monday lock-in per client (written by the autopilot)
create table if not exists public.lockins (
  client text primary key references public.clients(slug) on delete cascade,
  checked_at timestamptz, reason text, week_start date,
  account jsonb, weeks jsonb, items jsonb, media_checks jsonb, paused boolean
);

-- the error log (written by the autopilot)
create table if not exists public.log (
  id bigint generated always as identity primary key,
  client text not null references public.clients(slug) on delete cascade,
  key text,
  at timestamptz not null default now(),
  last_at timestamptz not null default now(),
  count int not null default 1,
  kind text not null, severity text not null, message text not null,
  post_id text, name text, date date, time text, platform text
);
create index if not exists log_client_idx on public.log(client, last_at desc);

-- when the autopilot last ran (one row)
create table if not exists public.autopilot_status (
  id int primary key default 1 check (id = 1),
  last_run_at timestamptz, ok boolean, message text
);

-- ------------------------------------------------- mark a client's schedule as changed
create or replace function public.posts_touch() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

create or replace function public.posts_changed() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  update public.clients set schedule_changed_at = now()
   where slug in (coalesce(new.client, old.client), coalesce(old.client, new.client));
  return null;
end $$;

drop trigger if exists posts_touch on public.posts;
create trigger posts_touch before update on public.posts for each row execute function public.posts_touch();
drop trigger if exists posts_changed on public.posts;
create trigger posts_changed after insert or update or delete on public.posts for each row execute function public.posts_changed();

-- ------------------------------------------- access: only signed-in app users, nobody else
alter table public.app_users        enable row level security;   -- no policies: not readable through the API
alter table public.clients          enable row level security;
alter table public.posts            enable row level security;
alter table public.occurrences      enable row level security;
alter table public.lockins          enable row level security;
alter table public.log              enable row level security;
alter table public.autopilot_status enable row level security;

drop policy if exists "app users read clients" on public.clients;
create policy "app users read clients" on public.clients for select to authenticated using (public.is_app_user());
drop policy if exists "app users update clients" on public.clients;
create policy "app users update clients" on public.clients for update to authenticated
  using (public.is_app_user()) with check (public.is_app_user());

drop policy if exists "app users manage posts" on public.posts;
create policy "app users manage posts" on public.posts for all to authenticated
  using (public.is_app_user()) with check (public.is_app_user());

drop policy if exists "app users read occurrences" on public.occurrences;
create policy "app users read occurrences" on public.occurrences for select to authenticated using (public.is_app_user());
drop policy if exists "app users read lockins" on public.lockins;
create policy "app users read lockins" on public.lockins for select to authenticated using (public.is_app_user());
drop policy if exists "app users read log" on public.log;
create policy "app users read log" on public.log for select to authenticated using (public.is_app_user());
drop policy if exists "app users read status" on public.autopilot_status;
create policy "app users read status" on public.autopilot_status for select to authenticated using (public.is_app_user());

-- the browser key ("anon"/publishable) gets nothing at all
revoke all on public.app_users, public.clients, public.posts, public.occurrences, public.lockins,
              public.log, public.autopilot_status from anon;

-- ------------------------------------------------------- private storage for the media
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('stories', 'stories', false, 52428800, array['image/jpeg','video/mp4','video/quicktime'])
on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "app users read stories" on storage.objects;
create policy "app users read stories" on storage.objects for select to authenticated
  using (bucket_id = 'stories' and public.is_app_user());
drop policy if exists "app users upload stories" on storage.objects;
create policy "app users upload stories" on storage.objects for insert to authenticated
  with check (bucket_id = 'stories' and public.is_app_user());
drop policy if exists "app users update stories" on storage.objects;
create policy "app users update stories" on storage.objects for update to authenticated
  using (bucket_id = 'stories' and public.is_app_user());
drop policy if exists "app users delete stories" on storage.objects;
create policy "app users delete stories" on storage.objects for delete to authenticated
  using (bucket_id = 'stories' and public.is_app_user());

-- ------------------------------------------------------------------- first client
insert into public.clients (slug, name, timezone, platforms, paused)
values ('gwc', 'Greta Workers Club', 'Australia/Sydney', '{instagram,facebook}', true)
on conflict (slug) do nothing;

-- ---------------------------------------------- give the existing login(s) access
-- Run this after creating your login. Later logins get no access unless added here.
insert into public.app_users (user_id) select id from auth.users on conflict do nothing;
