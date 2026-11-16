-- Bench Buddy: database setup.
-- Run this once in Supabase (SQL Editor -> paste -> Run).
-- It creates the tables, the photo/logo storage, and the security rules
-- that make sure each shop can only ever see its own data.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create type public.ticket_status as enum (
  'received', 'diagnosing', 'waiting_parts', 'ready', 'picked_up'
);

-- One row per shop. One login (owner) per shop in V1.
create table public.shops (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null unique references auth.users (id) on delete cascade,
  name text not null check (char_length(name) between 1 and 80),
  slug text not null unique check (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  email text,                       -- customers' replies go here
  phone text,
  logo_path text,                   -- file in the public "shop-logos" bucket
  labor_rate_cents integer not null default 8000 check (labor_rate_cents >= 0),
  diagnostic_fee_cents integer not null default 0 check (diagnostic_fee_cents >= 0),
  parts_markup_pct numeric(6,2) not null default 30 check (parts_markup_pct >= 0),
  tax_rate_pct numeric(6,3) not null default 0 check (tax_rate_pct >= 0 and tax_rate_pct < 100),
  tax_labor boolean not null default false,   -- false = tax on parts only
  next_ticket_number integer not null default 1,
  created_at timestamptz not null default now()
);

create table public.tickets (
  id uuid primary key default gen_random_uuid(),
  shop_id uuid not null references public.shops (id) on delete cascade,
  number integer not null,
  -- Long random code used in the customer's "check my repair" link.
  public_token text not null unique default encode(extensions.gen_random_bytes(16), 'hex'),
  customer_name text not null,
  customer_email text,
  customer_phone text,
  device_type text not null,
  device_model text not null,
  problem text not null,
  status public.ticket_status not null default 'received',
  tech_notes text not null default '',
  -- Safety flags found by plain keyword matching at intake (no AI needed).
  safety_flags jsonb not null default '[]'::jsonb,
  -- AI results (see src/lib/ai). Null until generated.
  triage jsonb,
  guide jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (shop_id, number)
);
create index tickets_shop_status_idx on public.tickets (shop_id, status, created_at desc);

create table public.ticket_photos (
  id uuid primary key default gen_random_uuid(),
  ticket_id uuid not null references public.tickets (id) on delete cascade,
  shop_id uuid not null references public.shops (id) on delete cascade,
  path text not null,               -- file in the private "ticket-photos" bucket
  created_at timestamptz not null default now()
);
create index ticket_photos_ticket_idx on public.ticket_photos (ticket_id);

-- A ticket has at most one estimate ("quote") and one final bill ("final").
create table public.quotes (
  id uuid primary key default gen_random_uuid(),
  ticket_id uuid not null references public.tickets (id) on delete cascade,
  shop_id uuid not null references public.shops (id) on delete cascade,
  kind text not null check (kind in ('quote', 'final')),
  status text not null default 'draft' check (status in ('draft', 'approved', 'sent')),
  repair_title text not null default '',
  lines jsonb not null default '[]'::jsonb,
  customer_note text not null default '',
  ai_notes jsonb,                   -- assumptions the AI made, shown to the tech only
  -- The shop's rates are copied here so later settings changes never alter an old quote.
  labor_rate_cents integer not null,
  parts_markup_pct numeric(6,2) not null,
  tax_rate_pct numeric(6,3) not null,
  tax_labor boolean not null,
  subtotal_cents integer not null default 0,
  tax_cents integer not null default 0,
  total_cents integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (ticket_id, kind)
);

-- Tracks AI work in progress so the page can show "working..." and how long it took.
create table public.ai_jobs (
  ticket_id uuid not null references public.tickets (id) on delete cascade,
  shop_id uuid not null references public.shops (id) on delete cascade,
  kind text not null check (kind in ('triage', 'guide', 'quote')),
  status text not null check (status in ('running', 'done', 'error')),
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  duration_ms integer,
  primary key (ticket_id, kind)
);

-- Every customer email we send (or tried to send).
create table public.email_log (
  id uuid primary key default gen_random_uuid(),
  shop_id uuid not null references public.shops (id) on delete cascade,
  ticket_id uuid references public.tickets (id) on delete cascade,
  kind text not null,
  to_email text not null,
  subject text not null,
  status text not null check (status in ('sent', 'skipped', 'error')),
  detail text,
  created_at timestamptz not null default now()
);
create index email_log_ticket_idx on public.email_log (ticket_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

-- The shop that belongs to the logged-in user (null if none / not logged in).
create function public.my_shop_id() returns uuid
language sql stable security definer set search_path = ''
as $$
  select id from public.shops where owner_id = (select auth.uid())
$$;

-- Hands out the next ticket number for a shop (1, 2, 3 ...) safely,
-- even if two customers submit at the same moment. Server-only.
create function public.take_ticket_number(p_shop_id uuid) returns integer
language sql security definer set search_path = ''
as $$
  update public.shops
     set next_ticket_number = next_ticket_number + 1
   where id = p_shop_id
  returning next_ticket_number - 1
$$;
revoke execute on function public.take_ticket_number(uuid) from public, anon, authenticated;
grant execute on function public.take_ticket_number(uuid) to service_role;

create function public.touch_updated_at() returns trigger
language plpgsql set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end
$$;
create trigger tickets_touch before update on public.tickets
  for each row execute function public.touch_updated_at();
create trigger quotes_touch before update on public.quotes
  for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Security: Row Level Security.
-- The database itself refuses to show a row unless it belongs to your shop.
-- Customers never talk to the database directly: the public intake form and
-- status page go through our server, which uses a server-only key.
-- ---------------------------------------------------------------------------

alter table public.shops enable row level security;
alter table public.tickets enable row level security;
alter table public.ticket_photos enable row level security;
alter table public.quotes enable row level security;
alter table public.ai_jobs enable row level security;
alter table public.email_log enable row level security;

create policy "owner reads own shop" on public.shops
  for select to authenticated using (owner_id = (select auth.uid()));
create policy "owner creates own shop" on public.shops
  for insert to authenticated with check (owner_id = (select auth.uid()));
create policy "owner updates own shop" on public.shops
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));

create policy "shop reads own tickets" on public.tickets
  for select to authenticated using (shop_id = (select public.my_shop_id()));
create policy "shop updates own tickets" on public.tickets
  for update to authenticated
  using (shop_id = (select public.my_shop_id()))
  with check (shop_id = (select public.my_shop_id()));
create policy "shop deletes own tickets" on public.tickets
  for delete to authenticated using (shop_id = (select public.my_shop_id()));

create policy "shop reads own photos" on public.ticket_photos
  for select to authenticated using (shop_id = (select public.my_shop_id()));

create policy "shop manages own quotes" on public.quotes
  for all to authenticated
  using (shop_id = (select public.my_shop_id()))
  with check (
    shop_id = (select public.my_shop_id())
    and exists (
      select 1 from public.tickets t
      where t.id = ticket_id and t.shop_id = (select public.my_shop_id())
    )
  );

create policy "shop reads own ai jobs" on public.ai_jobs
  for select to authenticated using (shop_id = (select public.my_shop_id()));

create policy "shop reads own email log" on public.email_log
  for select to authenticated using (shop_id = (select public.my_shop_id()));

-- Logged-out visitors get nothing from these tables.
revoke all on public.shops, public.tickets, public.ticket_photos,
  public.quotes, public.ai_jobs, public.email_log from anon;

-- A shop owner may not hand their shop to someone else or renumber tickets.
revoke update on public.shops from authenticated;
grant update (name, slug, email, phone, logo_path, labor_rate_cents, diagnostic_fee_cents,
  parts_markup_pct, tax_rate_pct, tax_labor) on public.shops to authenticated;
revoke insert on public.shops from authenticated;
grant insert (owner_id, name, slug, email, phone, labor_rate_cents, diagnostic_fee_cents,
  parts_markup_pct, tax_rate_pct, tax_labor) on public.shops to authenticated;
revoke update on public.tickets from authenticated;
grant update (customer_name, customer_email, customer_phone, device_type, device_model,
  problem, status, tech_notes) on public.tickets to authenticated;

-- ---------------------------------------------------------------------------
-- File storage
-- ---------------------------------------------------------------------------

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  -- Customer photos: PRIVATE. Only viewable through short-lived links we create.
  ('ticket-photos', 'ticket-photos', false, 5242880, array['image/jpeg', 'image/png', 'image/webp']),
  -- Shop logos: public, because they show on the public intake page.
  ('shop-logos', 'shop-logos', true, 2097152, array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do nothing;

-- Files are stored as "<shop id>/<ticket id>/<file>". A shop may read its own folder.
create policy "shop reads own ticket photos" on storage.objects
  for select to authenticated
  using (
    bucket_id = 'ticket-photos'
    and (storage.foldername(name))[1] = (select public.my_shop_id())::text
  );
