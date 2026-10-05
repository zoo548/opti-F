create table if not exists api_cache (
  cache_key  text primary key,
  provider   text not null,
  response   jsonb not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null
);

create index if not exists api_cache_expires_idx on api_cache (expires_at);

alter table api_cache enable row level security;
