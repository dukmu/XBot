# Migration Policy

The migration must preserve existing users and dependent order rows.

Dirty email cleanup rules:
- Keep the first existing `ada@example.com` row unchanged.
- The duplicate user `u4` must become `ada+u4@example.com`.
- The null email user `u5` must become `missing+u5@example.invalid`.
- The blank email user `u6` must become `missing+u6@example.invalid`.
- Future writes must reject duplicate or null emails.

Schema rules:
- Add `users.status TEXT NOT NULL DEFAULT 'active'`.
- Preserve historical `created_at`.
- Preserve `orders.user_id` references to the same user ids.
- The migration should run in an explicit transaction and be safe to run twice.

Rollback:
- `rollback.sql` must execute after the migration.
- It must restore the pre-migration users schema shape: `id`, `email`, `name`, `created_at`.
- It must preserve the same number of user rows and order rows, including dependent orders for dirty users.
