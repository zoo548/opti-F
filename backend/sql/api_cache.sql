create table if not exists public.api_cache (
  cache_key text primary key,
  provider text,
  response jsonb,
  created_at timestamptz default now(),
  expires_at timestamptz
);

create index if not exists api_cache_expires_at_idx on public.api_cache (expires_at);
