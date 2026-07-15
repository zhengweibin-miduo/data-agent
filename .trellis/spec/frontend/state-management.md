# Frontend State Management

## Current Scope

Not applicable yet. There is no frontend runtime and therefore no local UI
state, global client state, URL state, or cached server state.

The module-level `app_config` and class-level client instances under `app/` are
backend process state; they do not establish a frontend state-management
pattern. Do not select Redux, Zustand, React Context, or a server-state library
in this spec without an implementation task that introduces and validates it.

## Evidence

No frontend state dependency, store, provider, reducer, or query cache exists in
the repository.
