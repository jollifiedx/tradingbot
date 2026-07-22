-- One-way freeze guard: the worker may ENGAGE the freeze, never release it.
--
-- Owner ruling 2026-07-21 (docs/decisions.md): a drift halt must survive a restart, so the
-- worker persists it by setting `settings.frozen = true`. That is a narrow, deliberate
-- amendment to CLAUDE.md invariant #2 ("UI mutates settings; worker reads settings") in
-- exactly one direction. This migration makes the "never release it" half a property of the
-- DATABASE rather than a promise made by application code -- the same mechanism-not-convention
-- standard applied to the append-only audit tables.
--
-- Attribution is the discriminator. `settings.updated_by` is a FK to auth.users; the worker is
-- not a user, so a system-initiated change is attributed to NULL (the seed row already uses
-- NULL, so this is the established convention: changed_by IS NULL = the bot acted,
-- non-NULL = the owner acted). Faking the owner's UID for a worker halt would corrupt
-- settings_history, which is precisely the record a postmortem depends on.
--
-- Note on threat model: api and worker currently share one Postgres role, so the database
-- cannot distinguish them by role. Attribution is therefore the strongest available signal,
-- and it is the one that matters: it blocks the realistic failure -- a worker "resetting to a
-- known state" or an auto-recovery path clearing a halt nobody acknowledged.

create function reject_system_unfreeze()
returns trigger
language plpgsql
as $$
begin
    if old.frozen and not new.frozen and new.updated_by is null then
        raise exception
            'the freeze flag may only be cleared by an attributed owner action; a system '
            '(NULL updated_by) actor may engage the freeze but never release it '
            '(owner ruling 2026-07-21)'
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

comment on function reject_system_unfreeze() is
    'Enforces the one-way freeze rule: an unattributed (system/worker) update may set frozen true but never back to false. Owner clears the freeze via the dashboard, which always carries her auth.uid().';

create trigger settings_reject_system_unfreeze
    before update on settings
    for each row
    execute function reject_system_unfreeze();

comment on trigger settings_reject_system_unfreeze on settings is
    'The worker can lock the door but holds no key: only an attributed owner action can set frozen = false.';
