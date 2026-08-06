# MySQL compatibility upgrades

The bootstrap `data_agent.sql` file only creates fresh databases. Deployments
that retain an existing MySQL volume must apply each newer script in this
directory once, in filename order, before starting the upgraded backend.

For the natural-language Query upgrade from the parent of PR #85, run:

```bash
mysql --database data_agent < docs/docker/mysql/migrations/20260806_add_agent_message_semantic_fingerprint.sql
```

The statement only adds a nullable metadata column and preserves all existing
conversation rows.
