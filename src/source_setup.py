"""Saved source setup and per-scope sync planning. No I/O or model calls."""
from __future__ import annotations
import copy
from urllib.parse import urlparse
from inputs import repo_entries

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
    """Save additive explicit source selections; blanks keep history and existing scopes."""
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
    if saved and saved['config'].get('exclude_repos'):
        saved = copy.deepcopy(saved)
        saved['config']['repos_or_org'] = ', '.join(v['repo'] for v in scopes(saved['config']).values() if v['source'] == 'github')
        saved['config']['exclude_repos'] = ''
    setup = copy.deepcopy(saved) if saved else {'revision': 1, 'config': canonical(inputs)}
    cfg = setup['config']
    if saved:
        # Add new scopes only. Blank inputs and stale removed scopes never widen access.
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
    if setup.get('pending_config'):
        cfg = setup.pop('pending_config')
        setup['config'] = cfg
        setup['revision'] += 1
    # Removal is explicit, never inferred from blank source fields. Replays are idempotent.
    remove = {x.lower() for x in repo_entries(inputs.get('remove_sources'))}
    if remove - {'jira', 'slack', 'docs', 'dms'}:
        raise ValueError('Stop reading sources: use Jira, Slack, Docs or DMs, separated by commas.')
    old_scopes = set(scopes(cfg))
    for name in remove:
        for field, value in {'jira': [('jira_projects', '')], 'slack': [('slack_channels', ''), ('include_dms', 'No')], 'docs': [('docs_source', 'None')], 'dms': [('include_dms', 'No')]}.get(name, []):
            cfg[field] = value
    removed = old_scopes - set(scopes(cfg))
    if removed:
        setup['removed_scopes'] = sorted(set(setup.get('removed_scopes', [])) | removed)
        setup['revision'] += 1
    setup.pop('preview', None)
    effective = {**inputs, **cfg}
    effective['exclude_repos'] = ''
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
