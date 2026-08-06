# MySQL compatibility upgrades

The bootstrap `data_agent.sql` file only creates fresh databases. Deployments
that retain an existing MySQL volume must apply each newer script in this
directory once, in filename order, before starting the upgraded backend.

For the natural-language Query upgrade from the parent of PR #85, run:

```bash
mysql --database "${MEMORY_DATABASE:?set this to memory.database}" \
  < docs/docker/mysql/migrations/20260806_add_agent_message_semantic_fingerprint.sql
```

The statement only adds a nullable metadata column and preserves all existing
conversation rows.
Set `MEMORY_DATABASE` to the deployed `memory.database` value; the migration
must run against the same schema that owns `agent_message`.

Existing volumes also need the SELECT-only account introduced by Query. Set the
actual `data_sync.dw_database`, configured Query username, and a newly generated
password. Identifiers are restricted before expansion; the password must not
contain a single quote or newline.

```bash
export DW_DATABASE="${DW_DATABASE:?set data_sync.dw_database}"
export QUERY_USER="${QUERY_USER:-data_agent_query}"
export QUERY_PASSWORD="${QUERY_PASSWORD:?set the injected Query password}"
printf '%s\n' "$DW_DATABASE" "$QUERY_USER" | grep -Eqv '^[A-Za-z0-9_]+$' && exit 1
printf '%s' "$QUERY_PASSWORD" | grep -Eq "['\r\n]" && exit 1
envsubst < docs/docker/mysql/migrations/20260806_add_readonly_query_user.sql.template \
  | mysql --protocol=tcp -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u root -p
```

Inject the same username and password into the Query DSN. Before enabling the
endpoint, verify the account can `SELECT` from `${DW_DATABASE}`, cannot write
there, and cannot read a control schema.
