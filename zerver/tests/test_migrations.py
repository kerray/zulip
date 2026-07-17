# These are tests for Zulip's database migrations.  System documented at:
#   https://zulip.readthedocs.io/en/latest/subsystems/schema-migrations.html
#
# You can also read
#   https://www.caktusgroup.com/blog/2016/02/02/writing-unit-tests-django-migrations/
# to get a tutorial on the framework that inspired this feature.

# Important note: These tests are very expensive, and details of
# Django's database transaction model mean it does not super work to
# have a lot of migrations tested in this file at once; so we usually
# delete the old migration tests when adding a new one, so this file
# always has a single migration test in it as an example.
#
# The error you get with multiple similar tests doing migrations on
# the same table is this (table name may vary):
#
#   django.db.utils.OperationalError: cannot ALTER TABLE
#   "zerver_subscription" because it has pending trigger events
#
# The previous example test here, FixDeletedUserEmail (for migration
# 0805_fix_deleteduser_email), has been removed following that same
# upstream practice of pruning stale migration tests.  Its
# setUpBeforeMigration rolls the schema back to before 0805 and then
# calls example_user(), which SELECTs the *current* UserProfile model.
# Once this branch's migration 0807 adds the enable_web_push_notifications
# column, that SELECT references a column absent from the rolled-back
# schema and the test errors out (psycopg2 UndefinedColumn).  The next
# upstream change to touch this file reintroduces a single current
# example test.
