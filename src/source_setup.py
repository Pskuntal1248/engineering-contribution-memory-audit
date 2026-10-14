"""Saved source setup and per-scope sync planning. No I/O or model calls."""
from __future__ import annotations
import copy
from urllib.parse import urlparse
from inputs import repo_entries

ACTIONS = {'update_source_setup', 'enrich_new_scopes'}
FIELDS = ('repos_or_org', 'exclude_repos', 'jira_site', 'jira_projects', 'slack_channels', 'include_dms', 'slack_user_ids', 'docs_source', 'display_name', 'github_username')


def canonical(inp):
    cfg = {k: str(inp.get(k) or '') for k in FIELDS}
    cfg['repos_or_org'] = ', '.join(sorted({r.lower() for r in repo_entries(cfg['repos_or_org'])}))
    cfg['exclude_repos'] = ', '.join(sorted({r.lower() for r in repo_entries(cfg['exclude_repos'])}))
    cfg['jira_projects'] = ', '.join(sorted({p.upper() for p in repo_entries(cfg['jira_projects'])}))
    cfg['slack_channels'] = ', '.join(sorted({p.lstrip('#') for p in repo_entries(cfg['slack_channels'])}))
    cfg['include_dms'] = 'Yes' if cfg['include_dms'].lower() == 'yes' else 'No'
    cfg['docs_source'] = cfg['docs_source'] or 'None'
    return cfg


def scopes(config):
    excluded = {r.lower() for r in repo_entries(config.get('exclude_repos'))}
    out = {}
    for repo in repo_entries(config.get('repos_or_org')):
        if repo.lower() not in excluded:
            out['github:' + repo.lower()] = {'source': 'github', 'repo': repo}
    for key in repo_entries(config.get('jira_projects')):
        out['jira:' + config.get('jira_site', '').lower().rstrip('/') + ':' + key.upper()] = {'source': 'jira', 'project': key.upper()}
    # DM opt-in changes the scope key, so an old public-channel sync is never reused for a newly enabled DM.
    for channel in repo_entries(config.get('slack_channels')):
        out['slack:' + channel + ':' + config.get('include_dms', 'No')] = {'source': 'slack', 'channel': channel}
    docs = config.get('docs_source', '')
    names = ['confluence', 'notion', 'google drive'] if docs.lower() == 'all three' else [x.strip().lower() for x in docs.split('+') if x.strip().lower() != 'none']
    for name in names:
        out['docs:' + name] = {'source': 'docs', 'docs': name}
    return out


def resolve(prior, inputs):
    """Normal/scheduled inputs never overwrite saved scope. Explicit replacement requires current revision."""
    saved = prior.get('source_setup')
    if not saved and prior.get('last_run'):
        # Older code recorded explicit selections but not a shared setup. Migrate those selections, never broad discovery.
        legacy = dict(inputs)
        selected = prior.get('selected_repos') or (prior.get('repo_scope', {}).get('read') if prior.get('repo_scope', {}).get('mode') == 'allow-list' else None)
        if selected:
            legacy['repos_or_org'] = ', '.join(selected)
        for old, new in [('jira', 'jira_projects'), ('slack', 'slack_channels'), ('docs', 'docs_source')]:
            if old in prior.get('source_selection', {}):
                legacy[new] = prior['source_selection'][old]
        saved = {'revision': 1, 'config': canonical(legacy)}
    action = str(inputs.get('request') or 'weekly_update').strip().lower()
    if saved:
        setup = copy.deepcopy(saved)
        if action == 'update_source_setup':
            token = str(inputs.get('setup_revision') or '')
            if token != str(saved['revision']):
                raise ValueError(f"Source setup revision is {saved['revision']}; enter this in setup_revision to replace it. Stale scheduled inputs cannot change the setup.")
            cfg = canonical(inputs)
            for field in ('github_username', 'jira_site', 'display_name', 'slack_user_ids'):
                if not cfg.get(field):
                    cfg[field] = saved['config'].get(field, '')
            if cfg.get('github_username', '').lower() != saved['config'].get('github_username', '').lower():
                raise ValueError('Changing the GitHub identity of a saved profile is not supported. Source setup unchanged.')
            if saved['config'].get('jira_site') and cfg.get('jira_site') and saved['config']['jira_site'].rstrip('/').lower() != cfg['jira_site'].rstrip('/').lower():
                raise ValueError('Changing the Jira site of a saved profile is not supported. Source setup unchanged.')
            setup = {'revision': saved['revision'] + 1, 'config': cfg, 'removed_scopes': sorted(set(saved.get('removed_scopes', [])) | (set(scopes(saved['config'])) - set(scopes(cfg))))}
        else:
            cfg = setup['config']
    else:
        cfg = canonical(inputs)
        setup = {'revision': 1, 'config': cfg}
    if saved and action not in ACTIONS:
        # Capture only genuinely new additive scopes. Existing values and removals need explicit revision-checked replacement.
        candidate = canonical(inputs)
        additions = set(scopes(candidate)) - set(scopes(cfg)) - set(setup.get('removed_scopes', []))
        staged = copy.deepcopy(setup.get('pending_config') or cfg)
        for field, source in [('repos_or_org', 'github'), ('jira_projects', 'jira'), ('slack_channels', 'slack')]:
            fresh = [s for k, s in scopes(candidate).items() if k in additions and s['source'] == source]
            if fresh:
                if source == 'jira' and staged.get('jira_site') and candidate.get('jira_site') and staged['jira_site'].rstrip('/').lower() != candidate['jira_site'].rstrip('/').lower():
                    continue  # Different account/site is not an additive change.
                entries = repo_entries(staged.get(field))
                entries += [s['repo'] if source == 'github' else s['project'] if source == 'jira' else s['channel'] for s in fresh]
                staged[field] = ', '.join(sorted(set(entries)))
                if source == 'slack':
                    staged['include_dms'] = 'Yes' if staged.get('include_dms') == 'Yes' or candidate['include_dms'] == 'Yes' else 'No'
                    if candidate.get('slack_user_ids'):
                        staged['slack_user_ids'] = candidate['slack_user_ids']
                if source == 'jira' and not staged.get('jira_site'):
                    staged['jira_site'] = candidate['jira_site']
        if any(k in additions and s['source'] == 'docs' for k, s in scopes(candidate).items()):
            docs = {s['docs'] for s in scopes(staged).values() if s['source'] == 'docs'} | {s['docs'] for k, s in scopes(candidate).items() if k in additions and s['source'] == 'docs'}
            staged['docs_source'] = ' + '.join(sorted(docs))
        if staged != cfg:
            setup['pending_config'] = staged
    if action == 'enrich_new_scopes' and setup.get('pending_config'):
        cfg = setup.pop('pending_config')
        setup['config'] = cfg
        setup['revision'] += 1
    effective = {**inputs, **cfg}
    return effective, setup


def scoped_config(cfg, scope):
    """Only one explicitly selected scope in a read. Existing scopes cannot be refetched by a new-source enrichment."""
    from dataclasses import replace
    return replace(cfg, repos=[scope['repo']] if scope['source'] == 'github' else [],
                   jira_projects=[scope['project']] if scope['source'] == 'jira' else [],
                   slack_channels=[scope['channel']] if scope['source'] == 'slack' else [],
                   include_dms=cfg.include_dms if scope['source'] == 'slack' else False,
                   docs_sources=[scope['docs']] if scope['source'] == 'docs' else [])


def in_view(item, config):
    active = scopes(config)
    if item.source == 'github':
        return 'github:' + str(item.meta.get('repo', '')).lower() in active
    if item.source == 'jira':
        selected = {repo.upper() for repo in repo_entries(config.get('jira_projects'))}
        project = str(item.meta.get('project') or item.id.removeprefix('jira:').split('-')[0]).upper()
        site = config.get('jira_site', '')
        same_site = not site or urlparse(item.url).netloc.lower() == urlparse(site).netloc.lower()
        return project in selected and same_site
    if item.source == 'slack':
        channels = {p.lstrip('#') for p in repo_entries(config.get('slack_channels'))}
        return (item.meta.get('channel') in channels or item.meta.get('channel_id') in channels) and (not item.meta.get('is_dm') or config.get('include_dms') == 'Yes')
    if item.type == 'doc':
        prefix = item.id.split(':')[1].lower() if ':' in item.id else ''
        name = {'drive': 'google drive', 'gdrive': 'google drive'}.get(prefix, prefix)
        return 'docs:' + name in active
    return False


def scope_success(raw, spec):
    """A capped/unavailable/failed read is not complete, even if it returned some usable items."""
    if raw.get('blocked') or raw.get('failed') or raw.get('capped') or raw.get('github_incomplete'):
        return False
    source = spec['source']
    if source in {'jira', 'slack', 'docs'}:
        coverage = (raw.get('coverage') or {}).get(source, '')
        if any(t in coverage.lower() for t in ["isn't", 'failed', 'no ', 'unavailable', 'not connected']):
            return False
    return True
