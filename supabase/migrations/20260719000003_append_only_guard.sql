-- Generic append-only trigger function, applied to `decisions` and `orders`.
--
-- CLAUDE.md invariant #5 + non-negotiable constraint: decisions/orders must be impossible
-- to UPDATE or DELETE, not just discouraged by convention. RLS alone is not sufficient
-- because RLS is bypassed by the table owner and by the service_role key (which the API
-- and worker use for all normal writes) -- see
-- https://supabase.com/docs/guides/database/postgres/row-level-security#bypassing-row-level-security.
-- A BEFORE UPDATE/DELETE trigger fires for every role, including service_role and the
-- table owner, so it is the actual enforcement mechanism. RLS policies (added per-table)
-- are a second, redundant layer for the anon/authenticated paths.
--
-- Corrections to an append-only row are new rows referencing the old one (e.g. a
-- corrected decision references the superseded decision_id; a cancelled order is a new
-- order row with the terminal status, not a mutation of the original).

create function reject_update_or_delete()
returns trigger
language plpgsql
as $$
begin
    raise exception
        'table % is append-only: % is not permitted (attempted on row id=%)',
        tg_table_name, tg_op, coalesce(old.id::text, 'unknown')
        using errcode = '0A000'; -- feature_not_supported
    return null; -- unreachable, satisfies plpgsql return requirement
end;
$$;

comment on function reject_update_or_delete() is
    'Raises unconditionally. Attached as a BEFORE UPDATE OR DELETE trigger on append-only audit tables (decisions, orders) so mutation is impossible for every role, including service_role and the table owner.';
