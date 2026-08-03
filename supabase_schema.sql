-- StockPulz — Supabase schema.
-- Run once in the Supabase SQL editor, then set SUPABASE_URL + SUPABASE_KEY.
--
-- WHY TWO TABLES:
--   documents     — whole-blob files (picks, caches, global config). One row each.
--   user_records  — ONE ROW PER USER per file. This is what fixes the storage
--                   races the Gist could not: two processes writing DIFFERENT
--                   users now touch different rows (no whole-file rewrite to
--                   clobber), and same-user writes use the `version` column for
--                   real compare-and-swap — which the Gist API cannot do
--                   (PATCH rejects If-Match with 400).

create table if not exists documents (
    filename    text primary key,
    content     jsonb       not null,
    updated_at  timestamptz not null default now()
);

create table if not exists user_records (
    filename    text        not null,
    chat_id     text        not null,
    content     jsonb       not null,
    version     bigint      not null default 1,   -- optimistic concurrency token
    updated_at  timestamptz not null default now(),
    primary key (filename, chat_id)
);

create index if not exists user_records_filename_idx on user_records (filename);

-- Atomic compare-and-swap upsert.
-- Returns the new version on success, or NULL if `expected_version` did not
-- match (someone else wrote first) so the caller can re-run its mutation on
-- fresh data instead of silently clobbering.
create or replace function upsert_user_record(
    p_filename text,
    p_chat_id  text,
    p_content  jsonb,
    p_expected_version bigint
) returns bigint
language plpgsql
as $$
declare
    v_new bigint;
begin
    if p_expected_version is null then           -- insert-if-absent
        insert into user_records (filename, chat_id, content)
        values (p_filename, p_chat_id, p_content)
        on conflict (filename, chat_id) do nothing
        returning version into v_new;
        return v_new;                            -- NULL if it already existed
    end if;

    update user_records
       set content = p_content,
           version = version + 1,
           updated_at = now()
     where filename = p_filename
       and chat_id  = p_chat_id
       and version  = p_expected_version
    returning version into v_new;

    return v_new;                                -- NULL = version conflict
end;
$$;
