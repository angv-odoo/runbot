from __future__ import annotations

import ast
import collections
import contextlib
import datetime
import itertools
import json
import logging
import re
import time
from functools import reduce
from typing import Optional, Union, List, Iterator

import sentry_sdk
import werkzeug

from odoo import api, fields, models, tools, Command
from odoo.osv import expression
from odoo.tools import html_escape
from . import commands
from .utils import enum

from .. import github, exceptions, controllers, utils

_logger = logging.getLogger(__name__)


class StatusConfiguration(models.Model):
    _name = 'runbot_merge.repository.status'
    _description = "required statuses on repositories"
    _rec_name = 'context'
    _log_access = False

    context = fields.Char(required=True)
    repo_id = fields.Many2one('runbot_merge.repository', required=True, ondelete='cascade')
    branch_filter = fields.Char(help="branches this status applies to")
    prs = fields.Boolean(string="Applies to pull requests", default=True)
    stagings = fields.Boolean(string="Applies to stagings", default=True)

    def _for_branch(self, branch):
        assert branch._name == 'runbot_merge.branch', \
            f'Expected branch, got {branch}'
        return self.filtered(lambda st: (
            not st.branch_filter
            or branch.filtered_domain(ast.literal_eval(st.branch_filter))
        ))
    def _for_pr(self, pr):
        assert pr._name == 'runbot_merge.pull_requests', \
            f'Expected pull request, got {pr}'
        return self._for_branch(pr.target).filtered('prs')
    def _for_staging(self, staging):
        assert staging._name == 'runbot_merge.stagings', \
            f'Expected staging, got {staging}'
        return self._for_branch(staging.target).filtered('stagings')

class Repository(models.Model):
    _name = _description = 'runbot_merge.repository'
    _order = 'sequence, id'

    id: int

    sequence = fields.Integer(default=50, group_operator=None)
    name = fields.Char(required=True)
    project_id = fields.Many2one('runbot_merge.project', required=True, index=True)
    status_ids = fields.One2many('runbot_merge.repository.status', 'repo_id', string="Required Statuses")

    group_id = fields.Many2one('res.groups', default=lambda self: self.env.ref('base.group_user'))

    branch_filter = fields.Char(default='[(1, "=", 1)]', help="Filter branches valid for this repository")
    substitutions = fields.Text(
        "label substitutions",
        help="""sed-style substitution patterns applied to the label on input, one per line.

All substitutions are tentatively applied sequentially to the input.
""")

    @api.model
    def create(self, vals):
        if 'status_ids' in vals:
            return super().create(vals)

        st = vals.pop('required_statuses', 'legal/cla,ci/runbot')
        if st:
            vals['status_ids'] = [(0, 0, {'context': c}) for c in st.split(',')]
        return super().create(vals)

    def write(self, vals):
        st = vals.pop('required_statuses', None)
        if st:
            vals['status_ids'] = [(5, 0, {})] + [(0, 0, {'context': c}) for c in st.split(',')]
        return super().write(vals)

    def github(self, token_field='github_token') -> github.GH:
        return github.GH(self.project_id[token_field], self.name)

    def _auto_init(self):
        res = super(Repository, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_repo', self._table, ['name'])
        return res

    def _load_pr(self, number, *, closing=False):
        gh = self.github()

        # fetch PR object and handle as *opened*
        issue, pr = gh.pr(number)

        repo_name = pr['base']['repo']['full_name']
        if not self.project_id._has_branch(pr['base']['ref']):
            _logger.info("Tasked with loading %s PR %s#%d for un-managed branch %s:%s, ignoring",
                         pr['state'], repo_name, number, self.name, pr['base']['ref'])
            if not closing:
                self.env.ref('runbot_merge.pr.load.unmanaged')._send(
                    repository=self,
                    pull_request=number,
                    format_args = {
                        'pr': pr,
                        'repository': self,
                    },
                )
            return

        # if the PR is already loaded, force sync a few attributes
        pr_id = self.env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo_name),
            ('number', '=', number),
        ])
        if pr_id:
            sync = controllers.handle_pr(self.env, {
                'action': 'synchronize',
                'pull_request': pr,
                'sender': {'login': self.project_id.github_prefix}
            })
            edit = controllers.handle_pr(self.env, {
                'action': 'edited',
                'pull_request': pr,
                'changes': {
                    'base': {'ref': {'from': pr_id.target.name}},
                    'title': {'from': pr_id.message.splitlines()[0]},
                    'body': {'from', ''.join(pr_id.message.splitlines(keepends=True)[2:])},
                },
                'sender': {'login': self.project_id.github_prefix},
            })
            edit2 = ''
            if pr_id.draft != pr['draft']:
                edit2 = controllers.handle_pr(self.env, {
                    'action': 'converted_to_draft' if pr['draft'] else 'ready_for_review',
                    'pull_request': pr,
                    'sender': {'login': self.project_id.github_prefix}
                }) + '. '
            if pr_id.state != 'closed' and pr['state'] == 'closed':
                # don't go through controller because try_closing does weird things
                # for safety / race condition reasons which ends up committing
                # and breaks everything
                pr_id.state = 'closed'
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': pr_id.repository.id,
                'pull_request': number,
                'message': f"{edit}. {edit2}{sync}.",
            })
            return

        # special case for closed PRs, just ignore all events and skip feedback
        if closing:
            self.env['runbot_merge.pull_requests']._from_gh(pr)
            return

        sender = {'login': self.project_id.github_prefix}
        # init the PR to the null commit so we can later synchronise it back
        # back to the "proper" head while resetting reviews
        controllers.handle_pr(self.env, {
            'action': 'opened',
            'pull_request': {
                **pr,
                'head': {**pr['head'], 'sha': '0'*40},
                'state': 'open',
            },
            'sender': sender,
        })
        # fetch & set up actual head
        for st in gh.statuses(pr['head']['sha']):
            controllers.handle_status(self.env, st)
        # fetch and apply comments
        counter = itertools.count()
        items = [ # use counter so `comment` and `review` don't get hit during sort
            (comment['created_at'], next(counter), False, comment)
            for comment in gh.comments(number)
        ] + [
            (review['submitted_at'], next(counter), True, review)
            for review in gh.reviews(number)
        ]
        items.sort()
        for _, _, is_review, item in items:
            if is_review:
                controllers.handle_review(self.env, {
                    'action': 'submitted',
                    'review': item,
                    'pull_request': pr,
                    'repository': {'full_name': self.name},
                    'sender': sender,
                })
            else:
                controllers.handle_comment(self.env, {
                    'action': 'created',
                    'issue': issue,
                    'comment': item,
                    'repository': {'full_name': self.name},
                    'sender': sender,
                })
        # sync to real head
        controllers.handle_pr(self.env, {
            'action': 'synchronize',
            'pull_request': pr,
            'sender': sender,
        })
        pr_id = self.env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo_name),
            ('number', '=', number),
        ])
        if pr['state'] == 'closed':
            # don't go through controller because try_closing does weird things
            # for safety / race condition reasons which ends up committing
            # and breaks everything
            pr_id.closed = True

        self.env.ref('runbot_merge.pr.load.fetched')._send(
            repository=self,
            pull_request=number,
            format_args={'pr': pr_id},
        )

    def having_branch(self, branch):
        branches = self.env['runbot_merge.branch'].search
        return self.filtered(lambda r: branch in branches(ast.literal_eval(r.branch_filter)))

    def _remap_label(self, label):
        for line in filter(None, (self.substitutions or '').splitlines()):
            sep = line[0]
            _, pattern, repl, flags = line.split(sep)
            label = re.sub(
                pattern, repl, label,
                count=0 if 'g' in flags else 1,
                flags=(re.MULTILINE if 'm' in flags.lower() else 0)
                    | (re.IGNORECASE if 'i' in flags.lower() else 0)
            )
        return label

class Branch(models.Model):
    _name = _description = 'runbot_merge.branch'
    _order = 'sequence, name'

    id: int

    name = fields.Char(required=True)
    project_id = fields.Many2one('runbot_merge.project', required=True, index=True)

    active_staging_id = fields.Many2one(
        'runbot_merge.stagings', compute='_compute_active_staging', store=True, index=True,
        help="Currently running staging for the branch."
    )
    staging_ids = fields.One2many('runbot_merge.stagings', 'target')
    split_ids = fields.One2many('runbot_merge.split', 'target')

    prs = fields.One2many('runbot_merge.pull_requests', 'target', domain=[
        ('state', '!=', 'closed'),
        ('state', '!=', 'merged'),
    ])

    active = fields.Boolean(default=True)
    sequence = fields.Integer(group_operator=None)

    staging_enabled = fields.Boolean(default=True)

    def _auto_init(self):
        res = super(Branch, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_branch_per_repo',
            self._table, ['name', 'project_id'])
        return res

    @api.depends('active')
    def _compute_display_name(self):
        super()._compute_display_name()
        for b in self.filtered(lambda b: not b.active):
            b.display_name += ' (inactive)'

    def write(self, vals):
        super().write(vals)
        if vals.get('active') is False:
            self.active_staging_id.cancel(
                "Target branch deactivated by %r.",
                self.env.user.login,
            )
            tmpl = self.env.ref('runbot_merge.pr.branch.disabled')
            self.env['runbot_merge.pull_requests.feedback'].create([{
                'repository': pr.repository.id,
                'pull_request': pr.number,
                'message': tmpl._format(pr=pr),
            } for pr in self.prs])
            self.env.ref('runbot_merge.branch_cleanup')._trigger()
        return True

    @api.depends('staging_ids.active')
    def _compute_active_staging(self):
        for b in self:
            b.active_staging_id = b.with_context(active_test=True).staging_ids


ACL = collections.namedtuple('ACL', 'is_admin is_reviewer is_author')
class PullRequests(models.Model):
    _name = 'runbot_merge.pull_requests'
    _description = "Pull Request"
    _inherit = ['mail.thread']
    _order = 'number desc'
    _rec_name = 'number'

    id: int
    display_name: str

    target = fields.Many2one('runbot_merge.branch', required=True, index=True, tracking=True)
    repository = fields.Many2one('runbot_merge.repository', required=True)
    # NB: check that target & repo have same project & provide project related?

    closed = fields.Boolean(default=False, tracking=True)
    error = fields.Boolean(string="in error", default=False, tracking=True)
    skipchecks = fields.Boolean(related='batch_id.skipchecks')
    cancel_staging = fields.Boolean(related='batch_id.cancel_staging')
    merge_date = fields.Datetime(
        related='batch_id.merge_date',
        inverse=lambda _: 1/0,
        tracking=True,
        store=True,
    )

    state = fields.Selection([
        ('opened', 'Opened'),
        ('closed', 'Closed'),
        ('validated', 'Validated'),
        ('approved', 'Approved'),
        ('ready', 'Ready'),
        # staged?
        ('merged', 'Merged'),
        ('error', 'Error'),
    ],
        compute='_compute_state', store=True, default='opened',
        index=True, tracking=True, column_type=enum(_name, 'state'),
    )

    number = fields.Integer(required=True, index=True, group_operator=None)
    author = fields.Many2one('res.partner', index=True)
    head = fields.Char(required=True, tracking=True)
    label = fields.Char(
        required=True, index=True,
        help="Label of the source branch (owner:branchname), used for "
             "cross-repository branch-matching"
    )
    refname = fields.Char(compute='_compute_refname')
    message = fields.Text(required=True)
    draft = fields.Boolean(
        default=False, required=True, tracking=True,
        help="A draft PR can not be merged",
    )
    squash = fields.Boolean(default=False, tracking=True)
    merge_method = fields.Selection([
        ('merge', "merge directly, using the PR as merge commit message"),
        ('rebase-merge', "rebase and merge, using the PR as merge commit message"),
        ('rebase-ff', "rebase and fast-forward"),
        ('squash', "squash"),
    ], default=False, tracking=True, column_type=enum(_name, 'merge_method'))
    method_warned = fields.Boolean(default=False)

    reviewed_by = fields.Many2one('res.partner', index=True, tracking=True)
    delegates = fields.Many2many('res.partner', help="Delegate reviewers, not intrinsically reviewers but can review this PR")
    priority = fields.Selection(related="batch_id.priority", inverse=lambda _: 1 / 0)

    overrides = fields.Char(required=True, default='{}', tracking=True)
    statuses = fields.Text(help="Copy of the statuses from the HEAD commit, as a Python literal", default="{}")
    statuses_full = fields.Text(
        compute='_compute_statuses',
        help="Compilation of the full status of the PR (commit statuses + overrides), as JSON",
        store=True,
    )
    status = fields.Selection([
        ('pending', 'Pending'),
        ('failure', 'Failure'),
        ('success', 'Success'),
    ], compute='_compute_statuses', store=True, column_type=enum(_name, 'status'))
    previous_failure = fields.Char(default='{}')

    batch_id = fields.Many2one('runbot_merge.batch', index=True)
    staging_id = fields.Many2one('runbot_merge.stagings', compute='_compute_staging', store=True)

    @api.depends('batch_id.staging_ids.active')
    def _compute_staging(self):
        for p in self:
            p.staging_id = p.batch_id.staging_ids.filtered('active')

    commits_map = fields.Char(help="JSON-encoded mapping of PR commits to actually integrated commits. The integration head (either a merge commit or the PR's topmost) is mapped from the 'empty' pr commit (the key is an empty string, because you can't put a null key in json maps).", default='{}')

    link_warned = fields.Boolean(
        default=False, help="Whether we've already warned that this (ready)"
                            " PR is linked to an other non-ready PR"
    )

    blocked = fields.Char(
        compute='_compute_is_blocked', store=True,
        help="PR is not currently stageable for some reason (mostly an issue if status is ready)"
    )

    url = fields.Char(compute='_compute_url')
    github_url = fields.Char(compute='_compute_url')

    repo_name = fields.Char(related='repository.name')
    message_title = fields.Char(compute='_compute_message_title')

    ping = fields.Char(compute='_compute_ping', recursive=True)

    fw_policy = fields.Selection([
        ('default', "Default"),
        ('skipci', "Skip CI"),
    ], required=True, default="default", string="Forward Port Policy")
    source_id = fields.Many2one('runbot_merge.pull_requests', index=True, help="the original source of this FP even if parents were detached along the way")
    parent_id = fields.Many2one(
        'runbot_merge.pull_requests', index=True,
        help="a PR with a parent is an automatic forward port",
        tracking=True,
    )
    root_id = fields.Many2one('runbot_merge.pull_requests', compute='_compute_root', recursive=True)
    forwardport_ids = fields.One2many('runbot_merge.pull_requests', 'source_id')
    limit_id = fields.Many2one('runbot_merge.branch', help="Up to which branch should this PR be forward-ported", tracking=True)

    detach_reason = fields.Char()

    _sql_constraints = [(
        'fw_constraint',
        'check(source_id is null or num_nonnulls(parent_id, detach_reason) = 1)',
        "fw PRs must either be attached or have a reason for being detached",
    )]

    @api.depends('label')
    def _compute_refname(self):
        for pr in self:
            pr.refname = pr.label.split(':', 1)[-1]

    @api.depends(
        'author.github_login', 'reviewed_by.github_login',
        'source_id.author.github_login', 'source_id.reviewed_by.github_login',
    )
    def _compute_ping(self):
        for pr in self:
            if source := pr.source_id:
                contacts = source.author | source.reviewed_by | pr.reviewed_by
            else:
                contacts = pr.author | pr.reviewed_by

            s = ' '.join(f'@{p.github_login}' for p in contacts)
            pr.ping = s and (s + ' ')

    @api.depends('repository.name', 'number')
    def _compute_url(self):
        base = werkzeug.urls.url_parse(self.env['ir.config_parameter'].sudo().get_param('web.base.url', 'http://localhost:8069'))
        gh_base = werkzeug.urls.url_parse('https://github.com')
        for pr in self:
            path = f'/{werkzeug.urls.url_quote(pr.repository.name)}/pull/{pr.number}'
            pr.url = str(base.join(path))
            pr.github_url = str(gh_base.join(path))

    @api.depends('parent_id.root_id')
    def _compute_root(self):
        for p in self:
            p.root_id = reduce(lambda _, p: p, self._iter_ancestors())

    @api.depends('message')
    def _compute_message_title(self):
        for pr in self:
            pr.message_title = next(iter(pr.message.splitlines()), '')

    @api.depends('repository.name', 'number', 'message')
    def _compute_display_name(self):
        return super()._compute_display_name()

    def name_get(self):
        name_template = '%(repo_name)s#%(number)d'
        if self.env.context.get('pr_include_title'):
            name_template += ' (%(message_title)s)'
        return [(p.id, name_template % p) for p in self]

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        if not name or operator != 'ilike':
            return super().name_search(name, args=args, operator=operator, limit=limit)
        bits = [[('label', 'ilike', name)]]
        if name.isdigit():
            bits.append([('number', '=', name)])
        if re.match(r'\w+#\d+$', name):
            repo, num = name.rsplit('#', 1)
            bits.append(['&', ('repository.name', 'ilike', repo), ('number', '=', int(num))])
        else:
            bits.append([('repository.name', 'ilike', name)])
        domain = expression.OR(bits)
        if args:
            domain = expression.AND([args, domain])
        return self.search(domain, limit=limit).sudo().name_get()

    @property
    def _approved(self):
        return self.state in ('approved', 'ready')

    @property
    def _ready(self):
        return (self.squash or self.merge_method) and self._approved and self.status == 'success'

    @property
    def _linked_prs(self):
        return self.batch_id.prs - self

    @api.depends(
        'batch_id.prs.draft',
        'batch_id.prs.squash',
        'batch_id.prs.merge_method',
        'batch_id.prs.state',
        'batch_id.skipchecks',
    )
    def _compute_is_blocked(self):
        self.blocked = False
        requirements = (
            lambda p: not p.draft,
            lambda p: p.squash or p.merge_method,
            lambda p: p.state == 'ready' \
                  or p.batch_id.skipchecks \
                 and all(pr.state != 'error' for pr in p.batch_id.prs)
        )
        messages = ('is in draft', 'has no merge method', 'is not ready')
        for pr in self:
            if pr.state in ('merged', 'closed'):
                continue

            blocking, message = next((
                (blocking, message)
                for blocking in pr.batch_id.prs
                for requirement, message in zip(requirements, messages)
                if not requirement(blocking)
            ), (None, None))
            if blocking == pr:
                pr.blocked = message
            elif blocking:
                pr.blocked = f"linked PR {blocking.display_name} {message}"

    def _get_overrides(self) -> dict[str, dict[str, str]]:
        if self.parent_id:
            return self.parent_id._get_overrides() | json.loads(self.overrides)
        if self:
            return json.loads(self.overrides)
        return {}

    @api.depends('batch_ids.active')
    def _compute_active_batch(self):
        for r in self:
            r.batch_id = r.batch_ids.filtered(lambda b: b.active)[:1]

    def _get_or_schedule(self, repo_name, number, *, target=None, closing=False):
        repo = self.env['runbot_merge.repository'].search([('name', '=', repo_name)])
        if not repo:
            return

        if target and not repo.project_id._has_branch(target):
            self.env.ref('runbot_merge.pr.fetch.unmanaged')._send(
                repository=repo,
                pull_request=number,
                format_args={'repository': repo, 'branch': target, 'number': number}
            )
            return

        pr = self.search([
            ('repository', '=', repo.id),
            ('number', '=', number,)
        ])
        if pr:
            return pr

        Fetch = self.env['runbot_merge.fetch_job']
        if Fetch.search([('repository', '=', repo.id), ('number', '=', number)]):
            return
        Fetch.create({
            'repository': repo.id,
            'number': number,
            'closing': closing,
        })

    def _iter_ancestors(self) -> Iterator[PullRequests]:
        while self:
            yield self
            self = self.parent_id

    def _iter_descendants(self) -> Iterator[PullRequests]:
        pr = self
        while pr := self.search([('parent_id', '=', pr.id)]):
            yield pr

    def _parse_commands(self, author, comment, login):
        assert self, "parsing commands must be executed in an actual PR"

        (login, name) = (author.github_login, author.display_name) if author else (login, 'not in system')

        commandlines = self.repository.project_id._find_commands(comment['body'] or '')
        if not commandlines:
            _logger.info("found no commands in comment of %s (%s) (%s)", login, name,
                 utils.shorten(comment['body'] or '', 50)
            )
            return 'ok'

        def feedback(message: Optional[str] = None, close: bool = False):
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': self.repository.id,
                'pull_request': self.number,
                'message': message,
                'close': close,
            })
        try:
            cmds: List[commands.Command] = [
                ps
                for line in commandlines
                for ps in commands.Parser(line)
            ]
        except Exception as e:
            _logger.info(
                "error %s while parsing comment of %s (%s): %s",
                e,
                login, name,
                utils.shorten(comment['body'] or '', 50),
                exc_info=True
            )
            feedback(message=f"@{login} {e.args[0]}")
            return 'error'

        is_admin, is_reviewer, is_author = self._pr_acl(author)
        _source_admin, source_reviewer, source_author = self.source_id._pr_acl(author)

        if not (is_author or self.source_id or (any(isinstance(cmd, commands.Override) for cmd in cmds) and author.override_rights)):
            # no point even parsing commands
            _logger.info("ignoring comment of %s (%s): no ACL to %s",
                          login, name, self.display_name)
            self.env.ref('runbot_merge.command.access.no')._send(
                repository=self.repository,
                pull_request=self.number,
                format_args={'user': login, 'pr': self}
            )
            return 'ignored'

        rejections = []
        for command in cmds:
            msg = None
            match command:
                case commands.Approve() if self.draft:
                    msg = "draft PRs can not be approved."
                case commands.Approve() if self.parent_id:
                    # rules are a touch different for forwardport PRs:
                    valid = lambda _: True if command.ids is None else lambda n: n in command.ids
                    _, source_reviewer, source_author = self.source_id._pr_acl(author)

                    ancestors = list(self._iter_ancestors())
                    # - reviewers on the original can approve any forward port
                    if source_reviewer:
                        approveable = ancestors
                    else:
                        # between the first merged ancestor and self
                        mergeors = list(itertools.dropwhile(
                            lambda p: p.state != 'merged',
                            reversed(ancestors),
                        ))
                        # between the first ancestor the current user can review and self
                        reviewors = list(itertools.dropwhile(
                            lambda p: not p._pr_acl(author).is_reviewer,
                            reversed(ancestors),
                        ))

                        # source author can approve any descendant of a merged
                        # forward port (or source), people with review rights
                        # to a forward port have review rights to its
                        # descendants, if both apply use the most favorable
                        # (largest number of PRs)
                        if source_author and len(mergeors) > len(reviewors):
                            approveable = mergeors
                        else:
                            approveable = reviewors

                    if approveable:
                        for pr in approveable:
                            if not (pr.state in RPLUS and valid(pr.number)):
                                continue
                            msg = pr._approve(author, login)
                            if msg:
                                break
                    else:
                        msg = f"you can't {command} you silly little bean."
                case commands.Approve() if is_reviewer:
                    if command.ids is not None and command.ids != [self.number]:
                        msg = f"tried to approve PRs {command.ids} but the current PR is {self.number}"
                    else:
                        msg = self._approve(author, login)
                case commands.Reject() if is_author:
                    if self.batch_id.skipchecks or self.reviewed_by:
                        if self.error:
                            self.error = False
                        if self.reviewed_by:
                            self.reviewed_by = False
                        if self.batch_id.skipchecks:
                            self.batch_id.skipchecks = False
                            self.env.ref("runbot_merge.command.unapprove.p0")._send(
                                repository=self.repository,
                                pull_request=self.number,
                                format_args={'user': login, 'pr': self},
                            )
                        self.unstage("unreviewed (r-) by %s", login)
                    else:
                        msg = "r- makes no sense in the current PR state."
                case commands.MergeMethod() if is_reviewer:
                    self.merge_method = command.value
                    explanation = next(label for value, label in type(self).merge_method.selection if value == command.value)
                    self.env.ref("runbot_merge.command.method")._send(
                        repository=self.repository,
                        pull_request=self.number,
                        format_args={'new_method': explanation, 'pr': self, 'user': login},
                    )
                case commands.Retry() if is_author or source_author:
                    if self.error:
                        self.error = False
                    else:
                        msg = "retry makes no sense when the PR is not in error."
                case commands.Check() if is_author:
                    self.env['runbot_merge.fetch_job'].create({
                        'repository': self.repository.id,
                        'number': self.number,
                    })
                case commands.Delegate(users) if is_reviewer:
                    if not users:
                        delegates = self.author
                    else:
                        delegates = self.env['res.partner']
                        for login in users:
                            delegates |= delegates.search([('github_login', '=', login)]) or delegates.create({
                                'name': login,
                                'github_login': login,
                            })
                    delegates.write({'delegate_reviewer': [(4, self.id, 0)]})
                case commands.Priority() if is_admin:
                    self.batch_id.priority = str(command)
                case commands.SkipChecks() if is_admin:
                    self.batch_id.skipchecks = True
                    self.reviewed_by = author
                    for p in self.batch_id.prs - self:
                        if not p.reviewed_by:
                            p.reviewed_by = author
                case commands.CancelStaging() if is_admin:
                    self.batch_id.cancel_staging = True
                    if not self.batch_id.blocked:
                        self.target.active_staging_id.cancel(
                            "Unstaged by %s on %s",
                            author.github_login, self.display_name,
                        )
                case commands.Override(statuses):
                    for status in statuses:
                        overridable = author.override_rights\
                            .filtered(lambda r: not r.repository_id or (r.repository_id == self.repository))\
                            .mapped('context')
                        if status in overridable:
                            self.overrides = json.dumps({
                                **json.loads(self.overrides),
                                status: {
                                    'state': 'success',
                                    'target_url': comment['html_url'],
                                    'description': f"Overridden by @{author.github_login}",
                                },
                            })
                            c = self.env['runbot_merge.commit'].search([('sha', '=', self.head)])
                            if c:
                                c.to_check = True
                            else:
                                c.create({'sha': self.head, 'statuses': '{}'})
                        else:
                            msg = f"you are not allowed to override {status!r}."
                # FW
                case commands.Close() if source_author:
                    feedback(close=True)
                case commands.FW():
                    if source_reviewer or is_reviewer:
                        (self.source_id or self).batch_id.fw_policy = command.name.lower()
                        match command:
                            case commands.FW.DEFAULT:
                                message = "Waiting for CI to create followup forward-ports."
                            case commands.FW.SKIPCI:
                                message = "Not waiting for CI to create followup forward-ports."
                            case commands.FW.SKIPMERGE:
                                message = "Not waiting for merge to create followup forward-ports."
                        feedback(message=message)
                    else:
                        msg = "you can't configure forward-port CI."
                case commands.Limit(branch) if is_author:
                    ping, msg = self._maybe_update_limit(branch or self.target.name)
                    if not ping:
                        feedback(message=msg)
                        msg = None
                case commands.Limit():
                    msg = "you can't set a forward-port limit."
                # NO!
                case _:
                    msg = f"you can't {command}. Skill issue."
            if msg is not None:
                rejections.append(msg)

        cmdstr = ', '.join(map(str, cmds))
        if not rejections:
            _logger.info("%s (%s) applied %s", login, name, cmdstr)
            self.env.cr.precommit.data['change-author'] = author.id
            return 'applied ' + cmdstr

        self.env.cr.rollback()
        rejections_list = ''.join(f'\n- {r}' for r in rejections)
        _logger.info("%s (%s) tried to apply %s%s", login, name, cmdstr, rejections_list)
        footer = '' if len(cmds) == len(rejections) else "\n\nFor your own safety I've ignored everything in your comment."
        if rejections_list:
            rejections = ' ' + rejections_list.removeprefix("\n- ") if rejections_list.count('\n- ') == 1 else rejections_list
            feedback(message=f"@{login}{rejections}{footer}")
        return 'rejected'

    def _approve(self, author, login):
        oldstate = self.state
        newstate = RPLUS.get(self.state)
        msg = None
        if not author.email:
            msg = "I must know your email before you can review PRs. Please contact an administrator."
        elif not newstate:
            msg = "this PR is already reviewed, reviewing it again is useless."
        else:
            self.reviewed_by = author
        _logger.debug(
            "r+ on %s by %s (%s->%s) status=%s message? %s",
            self.display_name, author.github_login,
            oldstate, newstate or oldstate,
            self.status, self.status == 'failure'
        )
        if self.status == 'failure':
            # the normal infrastructure is for failure and
            # prefixes messages with "I'm sorry"
            self.env.ref("runbot_merge.command.approve.failure")._send(
                repository=self.repository,
                pull_request=self.number,
                format_args={'user': login, 'pr': self},
            )
        return msg

    def message_post(self, **kw):
        if author := self.env.cr.precommit.data.get('change-author'):
            kw['author_id'] = author
        if message := self.env.cr.precommit.data.get('change-message'):
            kw['body'] = html_escape(message)
        return super().message_post(**kw)

    def _message_log(self, **kw):
        if author := self.env.cr.precommit.data.get('change-author'):
            kw['author_id'] = author
        if message := self.env.cr.precommit.data.get('change-message'):
            kw['body'] = html_escape(message)
        return super()._message_log(**kw)

    def _pr_acl(self, user):
        if not self:
            return ACL(False, False, False)

        is_admin = self.env['res.partner.review'].search_count([
            ('partner_id', '=', user.id),
            ('repository_id', '=', self.repository.id),
            ('review', '=', True) if self.author != user else ('self_review', '=', True),
        ]) == 1
        is_reviewer = is_admin or self in user.delegate_reviewer
        # TODO: should delegate reviewers be able to retry PRs?
        is_author = is_reviewer or self.author == user
        return ACL(is_admin, is_reviewer, is_author)

    def _validate(self, statuses):
        # could have two PRs (e.g. one open and one closed) at least
        # temporarily on the same head, or on the same head with different
        # targets
        updateable = self.filtered(lambda p: p.state != 'merged')
        updateable.statuses = statuses
        for pr in updateable:
            if pr.status == "failure":
                statuses = json.loads(pr.statuses_full)
                for ci in pr.repository.status_ids._for_pr(pr).mapped('context'):
                    status = statuses.get(ci) or {'state': 'pending'}
                    if status['state'] in ('error', 'failure'):
                        pr._notify_ci_new_failure(ci, status)

    def modified(self, fnames, create=False, before=False):
        """ By default, Odoo can't express recursive *dependencies* which is
        exactly what we need for statuses: they depend on the current PR's
        overrides, and the parent's overrides, and *its* parent's overrides, ...

        One option would be to create a stored computed field which accumulates
        the overrides as *fields* can be recursive, but...
        """
        if 'overrides' in fnames:
            descendants_or_self = self.concat(*self._iter_descendants())
            self.env.add_to_compute(self._fields['status'], descendants_or_self)
            self.env.add_to_compute(self._fields['statuses_full'], descendants_or_self)
            self.env.add_to_compute(self._fields['state'], descendants_or_self)
        super().modified(fnames, create, before)

    @api.depends(
        'statuses', 'overrides', 'target', 'parent_id',
        'repository.status_ids.context',
        'repository.status_ids.branch_filter',
        'repository.status_ids.prs',
    )
    def _compute_statuses(self):
        for pr in self:
            statuses = {**json.loads(pr.statuses), **pr._get_overrides()}

            pr.statuses_full = json.dumps(statuses, indent=4)

            st = 'success'
            for ci in pr.repository.status_ids._for_pr(pr):
                v = (statuses.get(ci.context) or {'state': 'pending'})['state']
                if v in ('error', 'failure'):
                    st = 'failure'
                    break
                if v == 'pending':
                    st = 'pending'
            pr.status = st

    @api.depends(
        "status", "reviewed_by", "closed", "error" ,
        "batch_id.merge_date",
        "batch_id.skipchecks",
    )
    def _compute_state(self):
        for pr in self:
            if pr.batch_id.merge_date:
                pr.state = 'merged'
            elif pr.closed:
                pr.state = "closed"
            elif pr.error:
                pr.state = "error"
            elif pr.batch_id.skipchecks: # skipchecks behaves as both approval and status override
                pr.state = "ready"
            else:
                states = ("opened", "approved", "validated", "ready")
                pr.state = states[bool(pr.reviewed_by) | ((pr.status == "success") << 1)]


    def _notify_ci_new_failure(self, ci, st):
        prev = json.loads(self.previous_failure)
        if not any(self._statuses_equivalent(st, v) for v in prev.values()):
            prev[ci] = st
            self.previous_failure = json.dumps(prev)
            self._notify_ci_failed(ci)

    def _notify_merged(self, gh, payload):
        deployment = gh('POST', 'deployments', json={
            'ref': self.head, 'environment': 'merge',
            'description': "Merge %s into %s" % (self.display_name, self.target.name),
            'task': 'merge',
            'auto_merge': False,
            'required_contexts': [],
        }).json()
        gh('POST', 'deployments/{}/statuses'.format(deployment['id']), json={
            'state': 'success',
            'target_url': 'https://github.com/{}/commit/{}'.format(
                self.repository.name,
                payload['sha'],
            ),
            'description': "Merged %s in %s at %s" % (
                self.display_name, self.target.name, payload['sha']
            )
        })

    def _statuses_equivalent(self, a, b):
        """ Check if two statuses are *equivalent* meaning the description field
        is ignored (check only state and target_url). This is because the
        description seems to vary even if the rest does not, and generates
        unnecessary notififcations as a result
        """
        return a.get('state') == b.get('state') \
           and a.get('target_url')  == b.get('target_url')

    def _notify_ci_failed(self, ci):
        # only report an issue of the PR is already approved (r+'d)
        if self.state == 'approved':
            self.env.ref("runbot_merge.failure.approved")._send(
                repository=self.repository,
                pull_request=self.number,
                format_args={'pr': self, 'status': ci}
            )

    def _auto_init(self):
        for field in self._fields.values():
            if not isinstance(field, fields.Selection) or field.column_type[0] == 'varchar':
                continue

            t = field.column_type[1]
            self.env.cr.execute("SELECT 1 FROM pg_type WHERE typname = %s", [t])
            if not self.env.cr.rowcount:
                self.env.cr.execute(
                    f"CREATE TYPE {t} AS ENUM %s",
                    [tuple(s for s, _ in field.selection)]
                )

        super()._auto_init()
        # incorrect index: unique(number, target, repository).
        tools.drop_index(self._cr, 'runbot_merge_unique_pr_per_target', self._table)
        # correct index:
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_pr_per_repo', self._table, ['repository', 'number'])
        self._cr.execute("CREATE INDEX IF NOT EXISTS runbot_merge_pr_head "
                         "ON runbot_merge_pull_requests "
                         "USING hash (head)")

    @property
    def _tagstate(self):
        if self.state == 'ready' and self.staging_id.heads:
            return 'staged'
        return self.state

    def _get_batch(self, *, target, label):
        Batches = self.env['runbot_merge.batch']
        if re.search(r':patch-\d+$', label):
            batch = Batches
        else:
            batch = Batches.search([
                ('merge_date', '=', False),
                ('target', '=', target),
                ('prs.label', '=', label),
            ])
        return batch or Batches.create({'target': target})

    @api.model
    def create(self, vals):
        vals['batch_id'] = self._get_batch(target=vals['target'], label=vals['label']).id

        pr = super().create(vals)
        c = self.env['runbot_merge.commit'].search([('sha', '=', pr.head)])
        pr._validate(c.statuses or '{}')

        if pr.state not in ('closed', 'merged'):
            self.env.ref('runbot_merge.pr.created')._send(
                repository=pr.repository,
                pull_request=pr.number,
                format_args={'pr': pr},
            )
        return pr

    def _from_gh(self, description, author=None, branch=None, repo=None):
        if repo is None:
            repo = self.env['runbot_merge.repository'].search([
                ('name', '=', description['base']['repo']['full_name']),
            ])
        if branch is None:
            branch = self.env['runbot_merge.branch'].with_context(active_test=False).search([
                ('name', '=', description['base']['ref']),
                ('project_id', '=', repo.project_id.id),
            ])
        if author is None:
            author = self.env['res.partner'].search([
                ('github_login', '=', description['user']['login']),
            ], limit=1)

        return self.env['runbot_merge.pull_requests'].create({
            'closed': description['state'] != 'open',
            'number': description['number'],
            'label': repo._remap_label(description['head']['label']),
            'author': author.id,
            'target': branch.id,
            'repository': repo.id,
            'head': description['head']['sha'],
            'squash': description['commits'] == 1,
            'message': utils.make_message(description),
            'draft': description['draft'],
        })

    def write(self, vals):
        if vals.get('squash'):
            vals['merge_method'] = False

        # when explicitly marking a PR as ready
        if vals.get('state') == 'ready':
            # skip validation
            self.batch_id.skipchecks = True
            # mark current user as reviewer
            vals.setdefault('reviewed_by', self.env.user.partner_id.id)
            for p in self.batch_id.prs - self:
                if not p.reviewed_by:
                    p.reviewed_by = self.env.user.partner_id.id

        for pr in self:
            if (t := vals.get('target')) is not None and pr.target.id != t:
                pr.unstage(
                    "target (base) branch was changed from %r to %r",
                    pr.target.display_name,
                    self.env['runbot_merge.branch'].browse(t).display_name,
                )

            if 'message' in vals:
                merge_method = vals['merge_method'] if 'merge_method' in vals else pr.merge_method
                if merge_method not in (False, 'rebase-ff') and pr.message != vals['message']:
                    pr.unstage("merge message updated")

        match vals.get('closed'):
            case True if not self.closed:
                vals['reviewed_by'] = False
                vals['batch_id'] = False
                if len(self.batch_id.prs) == 1:
                    self.env.cr.precommit.add(self.batch_id.unlink)
            case False if self.closed:
                vals['batch_id'] = self._get_batch(
                    target=vals.get('target') or self.target.id,
                    label=vals.get('label') or self.label,
                )
            case None if (new_target := vals.get('target')) and new_target != self.target.id:
                vals['batch_id'] = self._get_batch(
                    target=new_target,
                    label=vals.get('label') or self.label,
                )
                if len(self.batch_id.prs) == 1:
                    self.env.cr.precommit.add(self.batch_id.unlink)
        w = super().write(vals)

        newhead = vals.get('head')
        if newhead:
            c = self.env['runbot_merge.commit'].search([('sha', '=', newhead)])
            self._validate(c.statuses or '{}')
        return w

    def _check_linked_prs_statuses(self, commit=False):
        """ Looks for linked PRs where at least one of the PRs is in a ready
        state and the others are not, notifies the other PRs.

        :param bool commit: whether to commit the tnx after each comment
        """
        # similar to Branch.try_staging's query as it's a subset of that
        # other query's behaviour
        self.env.cr.execute("""
        SELECT
          array_agg(pr.id) AS match
        FROM runbot_merge_pull_requests pr
        WHERE
          -- exclude terminal states (so there's no issue when
          -- deleting branches & reusing labels)
              pr.state != 'merged'
          AND pr.state != 'closed'
        GROUP BY
            pr.target,
            CASE
                WHEN pr.label SIMILAR TO '%%:patch-[[:digit:]]+'
                    THEN pr.id::text
                ELSE pr.label
            END
        HAVING
          -- one of the batch's PRs should be ready & not marked
              bool_or(pr.state = 'ready' AND NOT pr.link_warned)
          -- one of the others should be unready
          AND bool_or(pr.state != 'ready')
        """)
        for [ids] in self.env.cr.fetchall():
            prs = self.browse(ids)
            ready = prs.filtered(lambda p: p.state == 'ready')
            unready = (prs - ready).sorted(key=lambda p: (p.repository.name, p.number))

            for r in ready:
                self.env.ref('runbot_merge.pr.linked.not_ready')._send(
                    repository=r.repository,
                    pull_request=r.number,
                    format_args={
                        'pr': r,
                        'siblings': ', '.join(map('{0.display_name}'.format, unready))
                    },
                )
                r.link_warned = True
                if commit:
                    self.env.cr.commit()

        # send feedback for multi-commit PRs without a merge_method (which
        # we've not warned yet)
        methods = ''.join(
            '* `%s` to %s\n' % pair
            for pair in type(self).merge_method.selection
            if pair[0] != 'squash'
        )
        for r in self.search([
            ('state', '=', 'ready'),
            ('squash', '=', False),
            ('merge_method', '=', False),
            ('method_warned', '=', False),
        ]):
            self.env.ref('runbot_merge.pr.merge_method')._send(
                repository=r.repository,
                pull_request=r.number,
                format_args={'pr': r, 'methods':methods},
            )
            r.method_warned = True
            if commit:
                self.env.cr.commit()

    def _build_merge_message(self, message: Union['PullRequests', str], related_prs=()) -> 'Message':
        # handle co-authored commits (https://help.github.com/articles/creating-a-commit-with-multiple-authors/)
        m = Message.from_message(message)
        if not is_mentioned(message, self):
            m.body += f'\n\ncloses {self.display_name}'

        for r in related_prs:
            if not is_mentioned(message, r, full_reference=True):
                m.headers.add('Related', r.display_name)

        if self.reviewed_by:
            m.headers.add('signed-off-by', self.reviewed_by.formatted_email)

        return m

    def unstage(self, reason, *args):
        """ If the PR is staged, cancel the staging. If the PR is split and
        waiting, remove it from the split (possibly delete the split entirely)
        """
        split = self.batch_id.split_id
        if len(split.batch_ids) == 1:
            # only the batch of this PR -> delete split
            split.unlink()
        else:
            # else remove this batch from the split
            self.batch_id.split_id = False

        self.staging_id.cancel('%s ' + reason, self.display_name, *args)

    def _try_closing(self, by):
        # ignore if the PR is already being updated in a separate transaction
        # (most likely being merged?)
        self.env.cr.execute('''
        SELECT id, state, batch_id FROM runbot_merge_pull_requests
        WHERE id = %s AND state != 'merged'
        FOR UPDATE SKIP LOCKED;
        ''', [self.id])
        r = self.env.cr.fetchone()
        if not r:
            return False

        self.env.cr.execute('''
        UPDATE runbot_merge_pull_requests
        SET closed=True, reviewed_by = null
        WHERE id = %s
        ''', [self.id])
        self.env.cr.commit()
        self.modified(['closed', 'reviewed_by', 'batch_id'])
        self.unstage("closed by %s", by)

        # PRs should leave their batch when *closed*, and batches must die when
        # no PR is linked
        old_batch = self.batch_id
        self.batch_id = False
        if not old_batch.prs:
            old_batch.unlink()
        return True

# state changes on reviews
RPLUS = {
    'opened': 'approved',
    'validated': 'ready',
}

_TAGS = {
    False: set(),
    'opened': {'seen 🙂'},
}
_TAGS['validated'] = _TAGS['opened'] | {'CI 🤖'}
_TAGS['approved'] = _TAGS['opened'] | {'r+ 👌'}
_TAGS['ready'] = _TAGS['validated'] | _TAGS['approved']
_TAGS['staged'] = _TAGS['ready'] | {'merging 👷'}
_TAGS['merged'] = _TAGS['ready'] | {'merged 🎉'}
_TAGS['error'] = _TAGS['opened'] | {'error 🙅'}
_TAGS['closed'] = _TAGS['opened'] | {'closed 💔'}
ALL_TAGS = set.union(*_TAGS.values())

class Tagging(models.Model):
    """
    Queue of tag changes to make on PRs.

    Several PR state changes are driven by webhooks, webhooks should return
    quickly, performing calls to the Github API would *probably* get in the
    way of that. Instead, queue tagging changes into this table whose
    execution can be cron-driven.
    """
    _name = _description = 'runbot_merge.pull_requests.tagging'

    repository = fields.Many2one('runbot_merge.repository', required=True)
    # store the PR number (not id) as we need a Tagging for PR objects
    # being deleted (retargeted to non-managed branches)
    pull_request = fields.Integer(group_operator=None)

    tags_remove = fields.Char(required=True, default='[]')
    tags_add = fields.Char(required=True, default='[]')

    def create(self, values):
        if values.pop('state_from', None):
            values['tags_remove'] = ALL_TAGS
        if 'state_to' in values:
            values['tags_add'] = _TAGS[values.pop('state_to')]
        if not isinstance(values.get('tags_remove', ''), str):
            values['tags_remove'] = json.dumps(list(values['tags_remove']))
        if not isinstance(values.get('tags_add', ''), str):
            values['tags_add'] = json.dumps(list(values['tags_add']))
        return super().create(values)

    def _send(self):
        # noinspection SqlResolve
        self.env.cr.execute("""
        SELECT
            t.repository as repo_id,
            t.pull_request as pr_number,
            array_agg(t.id) as ids,
            array_agg(t.tags_remove::json) as to_remove,
            array_agg(t.tags_add::json) as to_add
        FROM runbot_merge_pull_requests_tagging t
        GROUP BY t.repository, t.pull_request
        """)
        Repos = self.env['runbot_merge.repository']
        ghs = {}
        to_remove = []
        for repo_id, pr, ids, remove, add in self.env.cr.fetchall():
            repo = Repos.browse(repo_id)

            gh = ghs.get(repo)
            if not gh:
                gh = ghs[repo] = repo.github()

            # fold all grouped PRs'
            tags_remove, tags_add = set(), set()
            for minus, plus in zip(remove, add):
                tags_remove.update(minus)
                # need to remove minuses from to_add in case we get e.g.
                # -foo +bar; -bar +baz, if we don't remove the minus, we'll end
                # up with -foo +bar +baz instead of -foo +baz
                tags_add.difference_update(minus)
                tags_add.update(plus)

            try:
                gh.change_tags(pr, tags_remove, tags_add)
            except Exception:
                _logger.info(
                    "Error while trying to change the tags of %s#%s from %s to %s",
                    repo.name, pr, remove, add,
                )
            else:
                to_remove.extend(ids)
        self.browse(to_remove).unlink()

class Feedback(models.Model):
    """ Queue of feedback comments to send to PR users
    """
    _name = _description = 'runbot_merge.pull_requests.feedback'

    repository = fields.Many2one('runbot_merge.repository', required=True)
    # store the PR number (not id) as we may want to send feedback to PR
    # objects on non-handled branches
    pull_request = fields.Integer(group_operator=None)
    message = fields.Char()
    close = fields.Boolean()
    token_field = fields.Selection(
        [('github_token', "Mergebot")],
        default='github_token',
        string="Bot User",
        help="Token field (from repo's project) to use to post messages"
    )

    def _send(self):
        ghs = {}
        to_remove = []
        for f in self.search([]):
            repo = f.repository
            gh = ghs.get((repo, f.token_field))
            if not gh:
                gh = ghs[(repo, f.token_field)] = repo.github(f.token_field)

            try:
                message = f.message
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(message or '')
                    message = data.get('message')

                    if data.get('base'):
                        gh('PATCH', f'pulls/{f.pull_request}', json={'base': data['base']})

                    if f.close:
                        pr_to_notify = self.env['runbot_merge.pull_requests'].search([
                            ('repository', '=', repo.id),
                            ('number', '=', f.pull_request),
                        ])
                        if pr_to_notify:
                            pr_to_notify._notify_merged(gh, data)

                if f.close:
                    gh.close(f.pull_request)

                if message:
                    gh.comment(f.pull_request, message)
            except Exception:
                _logger.exception(
                    "Error while trying to %s %s#%s (%s)",
                    'close' if f.close else 'send a comment to',
                    repo.name, f.pull_request,
                    utils.shorten(f.message, 200)
                )
            else:
                to_remove.append(f.id)
        self.browse(to_remove).unlink()

class FeedbackTemplate(models.Model):
    _name = 'runbot_merge.pull_requests.feedback.template'
    _description = "str.format templates for feedback messages, no integration," \
                   "but that's their purpose"
    _inherit = ['mail.thread']

    template = fields.Text(tracking=True)
    help = fields.Text(readonly=True)

    def _format(self, **args):
        return self.template.format_map(args)

    def _send(self, *, repository: Repository, pull_request: int, format_args: dict, token_field: Optional[str] = None) -> Optional[Feedback]:
        try:
            feedback = {
                'repository': repository.id,
                'pull_request': pull_request,
                'message': self.template.format_map(format_args),
            }
            if token_field:
                feedback['token_field'] = token_field
            return self.env['runbot_merge.pull_requests.feedback'].create(feedback)
        except Exception:
            _logger.exception("Failed to render template %s", self.get_external_id())
            raise


class StagingCommits(models.Model):
    _name = 'runbot_merge.stagings.commits'
    _description = "Mergeable commits for stagings, always the actually merged " \
                   "commit, never a uniquifier"
    _log_access = False

    staging_id = fields.Many2one('runbot_merge.stagings', required=True)
    commit_id = fields.Many2one('runbot_merge.commit', index=True, required=True)
    repository_id = fields.Many2one('runbot_merge.repository', required=True)

    def _auto_init(self):
        super()._auto_init()
        # the same commit can be both head and tip (?)
        tools.create_unique_index(
            self.env.cr, self._table + "_unique",
            self._table, ['staging_id', 'commit_id']
        )
        # there should be one head per staging per repository, unless one is a
        # real head and one is a uniquifier head
        tools.create_unique_index(
            self.env.cr, self._table + "_unique_per_repo",
            self._table, ['staging_id', 'repository_id'],
        )


class StagingHeads(models.Model):
    _name = 'runbot_merge.stagings.heads'
    _description = "Staging heads, may be the staging's commit or may be a " \
                   "uniquifier (discarded on success)"
    _log_access = False

    staging_id = fields.Many2one('runbot_merge.stagings', required=True)
    commit_id = fields.Many2one('runbot_merge.commit', index=True, required=True)
    repository_id = fields.Many2one('runbot_merge.repository', required=True)

    def _auto_init(self):
        super()._auto_init()
        # the same commit can be both head and tip (?)
        tools.create_unique_index(
            self.env.cr, self._table + "_unique",
            self._table, ['staging_id', 'commit_id']
        )
        # there should be one head per staging per repository, unless one is a
        # real head and one is a uniquifier head
        tools.create_unique_index(
            self.env.cr, self._table + "_unique_per_repo",
            self._table, ['staging_id', 'repository_id'],
        )


class Commit(models.Model):
    """Represents a commit onto which statuses might be posted,
    independent of everything else as commits can be created by
    statuses only, by PR pushes, by branch updates, ...
    """
    _name = _description = 'runbot_merge.commit'
    _rec_name = 'sha'

    sha = fields.Char(required=True)
    statuses = fields.Char(help="json-encoded mapping of status contexts to states", default="{}")
    to_check = fields.Boolean(default=False)

    head_ids = fields.Many2many('runbot_merge.stagings', relation='runbot_merge_stagings_heads', column2='staging_id', column1='commit_id')
    commit_ids = fields.Many2many('runbot_merge.stagings', relation='runbot_merge_stagings_commits', column2='staging_id', column1='commit_id')
    pull_requests = fields.One2many('runbot_merge.pull_requests', compute='_compute_prs')

    def create(self, values):
        values['to_check'] = True
        r = super(Commit, self).create(values)
        return r

    def write(self, values):
        values.setdefault('to_check', True)
        r = super(Commit, self).write(values)
        return r

    def _notify(self):
        Stagings = self.env['runbot_merge.stagings']
        PRs = self.env['runbot_merge.pull_requests']
        # chances are low that we'll have more than one commit
        for c in self.search([('to_check', '=', True)]):
            try:
                c.to_check = False
                pr = PRs.search([('head', '=', c.sha)])
                if pr:
                    self.env.cr.precommit.data['change-message'] =\
                        f"statuses changed on {c.sha}"
                    pr._validate(c.statuses)

                stagings = Stagings.search([('head_ids.sha', '=', c.sha), ('state', '=', 'pending')])
                if stagings:
                    stagings._validate()
            except Exception:
                _logger.exception("Failed to apply commit %s (%s)", c, c.sha)
                self.env.cr.rollback()
            else:
                self.env.cr.commit()

    _sql_constraints = [
        ('unique_sha', 'unique (sha)', 'no duplicated commit'),
    ]

    def _auto_init(self):
        res = super(Commit, self)._auto_init()
        self._cr.execute("""
            CREATE INDEX IF NOT EXISTS runbot_merge_unique_statuses
            ON runbot_merge_commit
            USING hash (sha)
        """)
        self._cr.execute("""
            CREATE INDEX IF NOT EXISTS runbot_merge_to_process
            ON runbot_merge_commit ((1)) WHERE to_check
        """)
        return res

    def _compute_prs(self):
        for c in self:
            c.pull_requests = self.env['runbot_merge.pull_requests'].search([
                ('head', '=', self.sha),
            ])


class Stagings(models.Model):
    _name = _description = 'runbot_merge.stagings'

    target = fields.Many2one('runbot_merge.branch', required=True, index=True)

    batch_ids = fields.Many2many('runbot_merge.batch', context={'active_test': False})
    pr_ids = fields.One2many('runbot_merge.pull_requests', compute='_compute_prs')
    state = fields.Selection([
        ('success', 'Success'),
        ('failure', 'Failure'),
        ('pending', 'Pending'),
        ('cancelled', "Cancelled"),
        ('ff_failed', "Fast forward failed")
    ], default='pending', index=True)
    active = fields.Boolean(default=True)

    staged_at = fields.Datetime(default=fields.Datetime.now, index=True)
    timeout_limit = fields.Datetime(store=True, compute='_compute_timeout_limit')
    reason = fields.Text("Reason for final state (if any)")

    head_ids = fields.Many2many('runbot_merge.commit', relation='runbot_merge_stagings_heads', column1='staging_id', column2='commit_id')
    heads = fields.One2many('runbot_merge.stagings.heads', 'staging_id')
    commit_ids = fields.Many2many('runbot_merge.commit', relation='runbot_merge_stagings_commits', column1='staging_id', column2='commit_id')
    commits = fields.One2many('runbot_merge.stagings.commits', 'staging_id')

    statuses = fields.Binary(compute='_compute_statuses')
    statuses_cache = fields.Text()

    def write(self, vals):
        # don't allow updating the statuses_cache
        vals.pop('statuses_cache', None)

        if 'state' not in vals:
            return super().write(vals)

        previously_pending = self.filtered(lambda s: s.state == 'pending')
        super().write(vals)
        for staging in previously_pending:
            if staging.state != 'pending':
                super(Stagings, staging).write({
                    'statuses_cache': json.dumps(staging.statuses)
                })

        return True

    def name_get(self):
        return [
            (staging.id, "%d (%s, %s%s)" % (
                staging.id,
                staging.target.name,
                staging.state,
                (', ' + staging.reason) if staging.reason else '',
            ))
            for staging in self
        ]

    @api.depends('heads')
    def _compute_statuses(self):
        """ Fetches statuses associated with the various heads, returned as
        (repo, context, state, url)
        """
        heads = {h.commit_id: h.repository_id for h in self.mapped('heads')}
        all_heads = self.mapped('head_ids')

        for st in self:
            if st.statuses_cache:
                st.statuses = json.loads(st.statuses_cache)
                continue

            commits = st.head_ids.with_prefetch(all_heads._prefetch_ids)
            st.statuses = [
                (
                    heads[commit].name,
                    context,
                    status.get('state') or 'pending',
                    status.get('target_url') or ''
                )
                for commit in commits
                for context, status in json.loads(commit.statuses).items()
            ]

    # only depend on staged_at as it should not get modified, but we might
    # update the CI timeout after the staging have been created and we
    # *do not* want to update the staging timeouts in that case
    @api.depends('staged_at')
    def _compute_timeout_limit(self):
        for st in self:
            st.timeout_limit = fields.Datetime.to_string(
                  fields.Datetime.from_string(st.staged_at)
                + datetime.timedelta(minutes=st.target.project_id.ci_timeout)
            )

    @api.depends('batch_ids.prs')
    def _compute_prs(self):
        for staging in self:
            staging.pr_ids = staging.batch_ids.prs

    def _validate(self):
        for s in self:
            if s.state != 'pending':
                continue

            # maps commits to the statuses they need
            required_statuses = [
                (h.commit_id.sha, h.repository_id.status_ids._for_staging(s).mapped('context'))
                for h in s.heads
            ]
            # maps commits to their statuses
            cmap = {c.sha: json.loads(c.statuses) for c in s.head_ids}

            update_timeout_limit = False
            st = 'success'
            for head, reqs in required_statuses:
                statuses = cmap.get(head) or {}
                for v in map(lambda n: statuses.get(n, {}).get('state'), reqs):
                    if st == 'failure' or v in ('error', 'failure'):
                        st = 'failure'
                    elif v is None:
                        st = 'pending'
                    elif v == 'pending':
                        st = 'pending'
                        update_timeout_limit = True
                    else:
                        assert v == 'success'

            vals = {'state': st}
            if update_timeout_limit:
                vals['timeout_limit'] = fields.Datetime.to_string(datetime.datetime.now() + datetime.timedelta(minutes=s.target.project_id.ci_timeout))
                _logger.debug("%s got pending status, bumping timeout to %s (%s)", self, vals['timeout_limit'], cmap)
            s.write(vals)

    def action_cancel(self):
        w = self.env['runbot_merge.stagings.cancel'].create({
            'staging_id': self.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'target': 'new',
            'name': f'Cancel staging {self.id} ({self.target.name})',
            'view_mode': 'form',
            'res_model': w._name,
            'res_id': w.id,
        }

    def cancel(self, reason, *args):
        self = self.filtered('active')
        if not self:
            return False

        _logger.info("Cancelling staging %s: " + reason, self, *args)
        self.write({
            'active': False,
            'state': 'cancelled',
            'reason': reason % args,
        })
        return True

    def fail(self, message, prs=None):
        _logger.info("Staging %s failed: %s", self, message)
        self.env.cr.precommit.data['change-message'] =\
            f'staging {self.id} failed: {message}'
        prs = prs or self.batch_ids.prs
        prs.error = True
        for pr in prs:
           self.env.ref('runbot_merge.pr.staging.fail')._send(
               repository=pr.repository,
               pull_request=pr.number,
               format_args={'pr': pr, 'message': message},
           )

        self.write({
            'active': False,
            'state': 'failure',
            'reason': message,
        })
        return True

    def try_splitting(self):
        batches = len(self.batch_ids)
        if batches > 1:
            midpoint = batches // 2
            h, t = self.batch_ids[:midpoint], self.batch_ids[midpoint:]
            # NB: batches remain attached to their original staging
            sh = self.env['runbot_merge.split'].create({
                'target': self.target.id,
                'batch_ids': [Command.link(batch.id) for batch in h],
            })
            st = self.env['runbot_merge.split'].create({
                'target': self.target.id,
                'batch_ids': [Command.link(batch.id) for batch in t],
            })
            _logger.info("Split %s to %s (%s) and %s (%s)",
                         self, h, sh, t, st)
            self.write({
                'active': False,
                'state': 'failure',
                'reason': self.reason if self.state == 'failure' else 'timed out'
            })
            return True

        # single batch => the staging is an unredeemable failure
        if self.state != 'failure':
            # timed out, just mark all PRs (wheee)
            self.fail('timed out (>{} minutes)'.format(self.target.project_id.ci_timeout))
            return False

        # try inferring which PR failed and only mark that one
        for head in self.heads:
            required_statuses = set(head.repository_id.status_ids._for_staging(self).mapped('context'))

            statuses = json.loads(head.commit_id.statuses or '{}')
            reason = next((
                ctx for ctx, result in statuses.items()
                if ctx in required_statuses
                if result.get('state') in ('error', 'failure')
            ), None)
            if not reason:
                continue

            pr = next((pr for pr in self.batch_ids.prs if pr.repository == head.repository_id), None)

            status = statuses[reason]
            viewmore = ''
            if status.get('target_url'):
                viewmore = ' (view more at %(target_url)s)' % status
            if pr:
                self.fail("%s%s" % (reason, viewmore), pr)
            else:
                self.fail('%s on %s%s' % (reason, head.commit_id.sha, viewmore))
            return False

        # the staging failed but we don't have a specific culprit, fail
        # everything
        self.fail("unknown reason")

        return False

    def check_status(self):
        """
        Checks the status of an active staging:
        * merges it if successful
        * splits it if failed (or timed out) and more than 1 batch
        * marks the PRs as failed otherwise
        * ignores if pending (or cancelled or ff_failed but those should also
          be disabled)
        """
        logger = _logger.getChild('cron')
        if not self.active:
            logger.info("Staging %s is not active, ignoring status check", self)
            return

        logger.info("Checking active staging %s (state=%s)", self, self.state)
        project = self.target.project_id
        if self.state == 'success':
            gh = {repo.name: repo.github() for repo in project.repo_ids.having_branch(self.target)}
            self.env.cr.execute('''
            SELECT 1 FROM runbot_merge_pull_requests
            WHERE id in %s
            FOR UPDATE
            ''', [tuple(self.mapped('batch_ids.prs.id'))])
            try:
                with sentry_sdk.start_span(description="merge staging") as span:
                    span.set_tag("staging", self.id)
                    span.set_tag("branch", self.target.name)
                    self._safety_dance(gh, self.commits)
            except exceptions.FastForwardError as e:
                logger.warning(
                    "Could not fast-forward successful staging on %s:%s",
                    e.args[0], self.target.name,
                    exc_info=True
                )
                self.write({
                    'state': 'ff_failed',
                    'reason': str(e.__cause__ or e.__context__ or e)
                })
            else:
                self.env.cr.precommit.data['change-message'] =\
                    f'staging {self.id} succeeded'
                prs = self.mapped('batch_ids.prs')
                logger.info(
                    "%s FF successful, marking %s as merged",
                    self, prs
                )
                self.batch_ids.merge_date = fields.Datetime.now()

                pseudobranch = None
                if self.target == project.branch_ids[:1]:
                    pseudobranch = project._next_freeze()

                for pr in prs:
                    self.env['runbot_merge.pull_requests.feedback'].create({
                        'repository': pr.repository.id,
                        'pull_request': pr.number,
                        'message': json.dumps({
                            'sha': json.loads(pr.commits_map)[''],
                        }),
                        'close': True,
                    })
                    if pseudobranch:
                        self.env['runbot_merge.pull_requests.tagging'].create({
                            'repository': pr.repository.id,
                            'pull_request': pr.number,
                            'tags_add': json.dumps([pseudobranch]),
                        })
            finally:
                self.write({'active': False})
        elif self.state == 'failure' or self.is_timed_out():
            self.try_splitting()

    def is_timed_out(self):
        return fields.Datetime.from_string(self.timeout_limit) < datetime.datetime.now()

    def _safety_dance(self, gh, staging_commits: StagingCommits):
        """ Reverting updates doesn't work if the branches are protected
        (because a revert is basically a force push). So we can update
        REPO_A, then fail to update REPO_B for some reason, and we're hosed.

        To try and make this issue less likely, do the safety dance:

        * First, perform a dry run using the tmp branches (which can be
          force-pushed and sacrificed), that way if somebody pushed directly
          to REPO_B during the staging we catch it. If we're really unlucky
          they could still push after the dry run but...
        * An other issue then is that the github call sometimes fails for no
          noticeable reason (e.g. network failure or whatnot), if it fails
          on REPO_B when REPO_A has already been updated things get pretty
          bad. In that case, wait a bit and retry for now. A more complex
          strategy (including disabling the branch entirely until somebody
          has looked at and fixed the issue) might be necessary.
        """
        tmp_target = 'tmp.' + self.target.name
        # first force-push the current targets to all tmps
        for repo_name in staging_commits.mapped('repository_id.name'):
            g = gh[repo_name]
            g.set_ref(tmp_target, g.head(self.target.name))
        # then attempt to FF the tmp to the staging commits
        for c in staging_commits:
            gh[c.repository_id.name].fast_forward(tmp_target, c.commit_id.sha)
        # there is still a race condition here, but it's way
        # lower than "the entire staging duration"...
        for i, c in enumerate(staging_commits):
            for pause in [0.1, 0.3, 0.5, 0.9, 0]: # last one must be 0/falsy of we lose the exception
                try:
                    gh[c.repository_id.name].fast_forward(
                        self.target.name,
                        c.commit_id.sha
                    )
                except exceptions.FastForwardError:
                    if i and pause:
                        time.sleep(pause)
                        continue
                    raise
                else:
                    break

    @api.returns('runbot_merge.stagings')
    def for_heads(self, *heads):
        """Returns the staging(s) with all the specified heads. Heads should
        be unique git oids.
        """
        if not heads:
            return self.browse(())

        joins = ''.join(
            f'\nJOIN runbot_merge_stagings_heads h{i} ON h{i}.staging_id = s.id'
            f'\nJOIN runbot_merge_commit c{i} ON c{i}.id = h{i}.commit_id AND c{i}.sha = %s\n'
            for i in range(len(heads))
        )
        self.env.cr.execute(f"SELECT s.id FROM runbot_merge_stagings s {joins}", heads)
        stagings = self.browse(id for [id] in self.env.cr.fetchall())
        stagings.check_access_rights('read')
        stagings.check_access_rule('read')
        return stagings

    @api.returns('runbot_merge.stagings')
    def for_commits(self, *heads):
        """Returns the staging(s) with all the specified commits (heads which
        have actually been merged). Commits should be unique git oids.
        """
        if not heads:
            return self.browse(())

        joins = ''.join(
            f'\nJOIN runbot_merge_stagings_commits h{i} ON h{i}.staging_id = s.id'
            f'\nJOIN runbot_merge_commit c{i} ON c{i}.id = h{i}.commit_id AND c{i}.sha = %s\n'
            for i in range(len(heads))
        )
        self.env.cr.execute(f"SELECT s.id FROM runbot_merge_stagings s {joins}", heads)
        stagings = self.browse(id for [id] in self.env.cr.fetchall())
        stagings.check_access_rights('read')
        stagings.check_access_rule('read')
        return stagings

class Split(models.Model):
    _name = _description = 'runbot_merge.split'

    target = fields.Many2one('runbot_merge.branch', required=True)
    batch_ids = fields.One2many('runbot_merge.batch', 'split_id', context={'active_test': False})


class FetchJob(models.Model):
    _name = _description = 'runbot_merge.fetch_job'

    active = fields.Boolean(default=True)
    repository = fields.Many2one('runbot_merge.repository', required=True)
    number = fields.Integer(required=True, group_operator=None)
    closing = fields.Boolean(default=False)

    def _check(self, commit=False):
        """
        :param bool commit: commit after each fetch has been executed
        """
        while True:
            f = self.search([], limit=1)
            if not f:
                return

            self.env.cr.execute("SAVEPOINT runbot_merge_before_fetch")
            try:
                f.repository._load_pr(f.number, closing=f.closing)
            except Exception:
                self.env.cr.execute("ROLLBACK TO SAVEPOINT runbot_merge_before_fetch")
                _logger.exception("Failed to load pr %s, skipping it", f.number)
            finally:
                self.env.cr.execute("RELEASE SAVEPOINT runbot_merge_before_fetch")

            f.active = False
            if commit:
                self.env.cr.commit()


from .stagings_create import is_mentioned, Message
