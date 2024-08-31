import datetime
import logging
import multiprocessing
import os
import re
import time
from functools import partial
from pathlib import Path

import click
import jira

from . import (
    Arch,
    ArtifactJob,
    CLIContext,
    Compose,
    ErrataTool,
    ErratumContentType,
    Event,
    EventType,
    ExecuteJob,
    Execution,
    Issue,
    IssueConfig,
    IssueHandler,
    JiraJob,
    NVRParser,
    OnRespinAction,
    RawRecipeConfigDimension,
    RawRecipeReportPortalConfigDimension,
    Recipe,
    RecipeConfig,
    ReportPortal,
    ScheduleJob,
    Settings,
    TFRequest,
    eval_test,
    get_url_basename,
    render_template,
    )

JIRA_NONE_ID = '_NO_ISSUE'
STATEDIR_PARENT_DIR = Path('/var/tmp/newa')
STATEDIR_NAME_PATTERN = r'^run-([0-9]+)$'

logging.basicConfig(
    format='%(asctime)s %(message)s',
    datefmt='%m/%d/%Y %I:%M:%S %p',
    level=logging.INFO)


def get_state_dir(use_ppid: bool = False) -> Path:
    """ When not using ppid returns the first unused directory
        matching /var/tmp/newa/run-[0-9]+, starting with run-1
        When using ppid searches for the latest state-dir directory
        containing file $PPID.ppid
    """
    counter = 0
    ppid_filename = f'{os.getppid()}.ppid'
    try:
        obj = os.scandir(STATEDIR_PARENT_DIR)
    except FileNotFoundError as e:
        if use_ppid:
            raise Exception(f'{STATEDIR_PARENT_DIR} does not exist') from e
        # return initial value run-1
        return STATEDIR_PARENT_DIR / f'run-{counter+1}'
    # iterate through subdirectories and find the latest matching dir
    for entry in obj:
        r = re.match(STATEDIR_NAME_PATTERN, entry.name)
        if entry.is_dir() and r:
            c = int(r.group(1))
            if use_ppid:
                ppid_file = STATEDIR_PARENT_DIR / entry.name / ppid_filename
                if ppid_file.exists() and c > counter:
                    counter = c
            elif c > counter:
                counter = c
    # for use_ppid use the largest counter value when found
    if use_ppid:
        if counter:
            return STATEDIR_PARENT_DIR / f'run-{counter}'
        raise Exception(f'File {ppid_filename} not found under {STATEDIR_PARENT_DIR}')
    # otherwise return the first unused value
    return STATEDIR_PARENT_DIR / f'run-{counter+1}'


@click.group(chain=True)
@click.option(
    '--state-dir',
    default='',
    help='Specify state directory.',
    )
@click.option(
    '--prev-state-dir',
    is_flag=True,
    default=False,
    help='Use the latest state-dir used previously within this shell session',
    )
@click.option(
    '--conf-file',
    default='$HOME/.newa',
    help='Path to newa configuration file.',
    )
@click.option(
    '--debug',
    is_flag=True,
    default=False,
    help='Enable debug logging',
    )
@click.option(
    '-e', '--environment', 'envvars',
    default=[],
    multiple=True,
    help='Specify custom environment variable, e.g. "-e FOO=BAR".',
    )
@click.option(
    '-c', '--context', 'contexts',
    default=[],
    multiple=True,
    help='Specify custom tmt context, e.g. "-c foo=bar".',
    )
@click.pass_context
def main(click_context: click.Context,
         state_dir: str,
         prev_state_dir: bool,
         conf_file: str,
         debug: bool,
         envvars: list[str],
         contexts: list[str]) -> None:

    # handle state_dir settings
    if prev_state_dir and state_dir:
        raise Exception('Use either --state-dir or --prev-state-dir')
    if prev_state_dir:
        state_dir = str(get_state_dir(use_ppid=True))
    elif not state_dir:
        state_dir = str(get_state_dir())

    ctx = CLIContext(
        settings=Settings.load(Path(os.path.expandvars(conf_file))),
        logger=logging.getLogger(),
        state_dirpath=Path(os.path.expandvars(state_dir)),
        cli_environment={},
        cli_context={},
        )
    click_context.obj = ctx

    if debug:
        ctx.logger.setLevel(logging.DEBUG)
    ctx.logger.info(f'Using --state-dir={ctx.state_dirpath}')
    if not ctx.state_dirpath.exists():
        ctx.logger.debug(f'State directory {ctx.state_dirpath} does not exist, creating...')
        ctx.state_dirpath.mkdir(parents=True)
    # create empty ppid file
    with open(os.path.join(ctx.state_dirpath, f'{os.getppid()}.ppid'), 'w'):
        pass

    def _split(s: str) -> tuple[str, str]:
        """ split key='some value' into a tuple (key, value) """
        r = re.match(r"""^\s*([a-zA-Z0-9_][a-zA-Z0-9_\-]*)=["']?(.*?)["']?\s*$""", s)
        if not r:
            raise Exception(
                f'Option value {s} has invalid format, key=value format expected!')
        k, v = r.groups()
        return (k, v)

    # store environment variables and context provided on a cmdline
    ctx.cli_environment.update(dict(_split(s) for s in envvars))
    ctx.cli_context.update(dict(_split(s) for s in contexts))


@main.command(name='event')
@click.option(
    '-e', '--erratum', 'errata_ids',
    default=[],
    multiple=True,
    help='Specifies erratum-type event for a given advisory ID.',
    )
@click.option(
    '-c', '--compose', 'compose_ids',
    default=[],
    multiple=True,
    help='Specifies compose-type event for a given compose.',
    )
@click.pass_obj
def cmd_event(ctx: CLIContext, errata_ids: list[str], compose_ids: list[str]) -> None:
    ctx.enter_command('event')

    # Errata IDs were not given, try to load them from init- files.
    if not errata_ids and not compose_ids:
        events = [e.event for e in ctx.load_initial_errata('init-')]
        for event in events:
            if event.type_ is EventType.ERRATUM:
                errata_ids.append(event.id)
            if event.type_ is EventType.COMPOSE:
                compose_ids.append(event.id)

    if not errata_ids and not compose_ids:
        raise Exception('Missing event IDs!')

    # process errata IDs
    if errata_ids:
        # Abort if there are still no errata IDs.
        et_url = ctx.settings.et_url
        if not et_url:
            raise Exception('Errata Tool URL is not configured!')

        for erratum_id in errata_ids:
            event = Event(type_=EventType.ERRATUM, id=erratum_id)
            errata = ErrataTool(url=et_url).get_errata(event)
            for erratum in errata:
                # identify compose to be used, just a dump conversion for now
                compose = erratum.release.strip()
                if compose.endswith('.GA'):
                    compose = compose[:-3]
                compose += '-Nightly'
                # handle compose differences between ET and TF
                compose = compose.replace('RHEL-10.0.BETA', 'RHEL-10-Beta')
                if erratum.content_type in (ErratumContentType.RPM, ErratumContentType.MODULE):
                    artifact_job = ArtifactJob(event=event, erratum=erratum,
                                               compose=Compose(id=compose))
                    ctx.save_artifact_job('event-', artifact_job)
                # for docker content type we create ArtifactJob per build
                if erratum.content_type == ErratumContentType.DOCKER:
                    erratum_clone = erratum.clone()
                    for build in erratum.builds:
                        erratum_clone.builds = [build]
                        erratum_clone.components = [NVRParser(build).name]
                        artifact_job = ArtifactJob(event=event, erratum=erratum_clone,
                                                   compose=Compose(id=compose))
                        ctx.save_artifact_job('event-', artifact_job)

    # process compose IDs
    for compose_id in compose_ids:
        event = Event(type_=EventType.COMPOSE, id=compose_id)
        artifact_job = ArtifactJob(event=event, erratum=None, compose=Compose(id=compose_id))
        ctx.save_artifact_job('event-', artifact_job)


@main.command(name='jira')
@click.option(
    '--issue-config',
    help='Specifies path to a Jira issue configuration file.',
    )
@click.option(
    '--recreate',
    is_flag=True,
    default=False,
    help='Instructs newa to ignore closed isseus and created new ones.',
    )
@click.option(
    '--issue',
    help='Specifies Jira issue ID to be used.',
    )
@click.option(
    '--job-recipe',
    help='Specifies job recipe file or URL to be used.',
    )
@click.option(
    '--assignee', 'assignee',
    help='Overrides Jira assignee from the issue config file.',
    default=None,
    )
@click.option(
    '--unassigned',
    is_flag=True,
    default=False,
    help='Create unassigned Jira issues, overriding values from the issue config file.',
    )
@click.pass_obj
def cmd_jira(
        ctx: CLIContext,
        issue_config: str,
        recreate: bool,
        issue: str,
        job_recipe: str,
        assignee: str,
        unassigned: bool) -> None:
    ctx.enter_command('jira')

    jira_url = ctx.settings.jira_url
    if not jira_url:
        raise Exception('Jira URL is not configured!')

    jira_token = ctx.settings.jira_token
    if not jira_token:
        raise Exception('Jira token is not configured!')

    if assignee and unassigned:
        raise Exception('Options --assignee and --unassigned cannot be used together')

    for artifact_job in ctx.load_artifact_jobs('event-'):
        # when issue_config is defined, --issue and --job-recipe are ignored
        # as it will be set depending on the --issue-config content
        if issue_config:
            # read Jira issue configuration
            config = IssueConfig.from_yaml_with_include(os.path.expandvars(issue_config))

            jira_handler = IssueHandler(
                artifact_job,
                jira_url,
                jira_token,
                config.project,
                config.transitions,
                group=getattr(
                    config,
                    'group',
                    None))
            ctx.logger.info("Initialized Jira handler")

            # All issue action from the configuration.
            issue_actions = config.issues[:]

            # Processed action (action.id : issue).
            processed_actions: dict[str, Issue] = {}

            # action_ids for which new Issues have been created
            created_action_ids: list[str] = []

            # Length of the queue the last time issue action was processed,
            # Use to prevent endless loop over the issue actions.
            endless_loop_check: dict[str, int] = {}

            # Iterate over issue actions. Take one, if it's not possible to finish it,
            # put it back at the end of the queue.
            while issue_actions:
                action = issue_actions.pop(0)

                ctx.logger.info(f"Processing {action.id}")

                if action.when and not eval_test(action.when,
                                                 JOB=artifact_job,
                                                 EVENT=artifact_job.event,
                                                 ERRATUM=artifact_job.erratum,
                                                 COMPOSE=artifact_job.compose,
                                                 ENVIRONMENT=ctx.cli_environment):
                    ctx.logger.info(f"Skipped, issue action is irrelevant ({action.when})")
                    continue

                rendered_summary = render_template(
                    action.summary,
                    ERRATUM=artifact_job.erratum,
                    COMPOSE=artifact_job.compose,
                    ENVIRONMENT=ctx.cli_environment)
                rendered_description = render_template(
                    action.description,
                    ERRATUM=artifact_job.erratum,
                    COMPOSE=artifact_job.compose,
                    ENVIRONMENT=ctx.cli_environment)
                if assignee:
                    rendered_assignee = assignee
                elif unassigned:
                    rendered_assignee = None
                elif action.assignee:
                    rendered_assignee = render_template(
                        action.assignee,
                        ERRATUM=artifact_job.erratum,
                        COMPOSE=artifact_job.compose,
                        ENVIRONMENT=ctx.cli_environment)
                else:
                    rendered_assignee = None
                if action.newa_id:
                    action.newa_id = render_template(
                        action.newa_id,
                        ERRATUM=artifact_job.erratum,
                        COMPOSE=artifact_job.compose,
                        ENVIRONMENT=ctx.cli_environment)

                # Detect that action has parent available (if applicable), if we went trough the
                # actions already and parent was not found, we abort.
                if action.parent_id and action.parent_id not in processed_actions:
                    queue_length = len(issue_actions)
                    last_queue_length = endless_loop_check.get(action.id, 0)
                    if last_queue_length == queue_length:
                        raise Exception(f"Parent {action.parent_id} for {action.id} not found!")

                    endless_loop_check[action.id] = queue_length
                    ctx.logger.info(f"Skipped for now (parent {action.parent_id} not yet found)")

                    issue_actions.append(action)
                    continue

                # Find existing issues related to artifact_job and action
                # If we are supposed to recreate closed issues, search only for opened ones
                if recreate:
                    search_result = jira_handler.get_related_issues(
                        action, all_respins=True, closed=False)
                else:
                    search_result = jira_handler.get_related_issues(
                        action, all_respins=True, closed=True)

                # Issues related to the curent respin and previous one(s).
                new_issues: list[Issue] = []
                old_issues: list[Issue] = []
                for jira_issue_key, jira_issue in search_result.items():
                    ctx.logger.info(f"Checking {jira_issue_key}")

                    # In general, issue is new (relevant to the current respin) if it has newa_id
                    # of this action in the description. Otherwise, it is old (relevant to the
                    # previous respins).
                    #
                    # However, it might happen that we encounter an issue that is new but its
                    # original parent has been replaced by a newly created issue. In such a case
                    # we have to re-create the issue as well and drop the old one.
                    is_new = False
                    if jira_handler.newa_id(action) in jira_issue["description"] \
                        and (not action.parent_id
                             or action.parent_id not in created_action_ids):
                        is_new = True

                    if is_new:
                        new_issues.append(
                            Issue(
                                jira_issue_key,
                                group=config.group,
                                closed=jira_issue["status"] == "closed"))
                    # opened old issues may be reused
                    elif jira_issue["status"] == "opened":
                        old_issues.append(
                            Issue(
                                jira_issue_key,
                                group=config.group,
                                closed=False))

                # Old opened issue(s) can be re-used for the current respin.
                if old_issues and action.on_respin == OnRespinAction.KEEP:
                    new_issues.extend(old_issues)
                    old_issues = []

                # Unless we want recreate closed issues we would stop processing
                # if new_issues are closed as it means they are already processed by a user
                if new_issues and (not recreate):
                    opened_issues = [i for i in new_issues if not i.closed]
                    closed_issues = [i for i in new_issues if i.closed]
                    # if there are no opened new issues we are done processing
                    if not opened_issues:
                        closed_ids = ', '.join([i.id for i in closed_issues])
                        ctx.logger.info(
                            f"Relevant issues {closed_ids} found but already closed")
                        continue
                    # otherwise we continue processing new issues
                    new_issues = opened_issues

                # Processing new opened issues.
                #
                # 1. Either there is no new issue (it does not exist yet - we need to create it).
                if not new_issues:
                    parent = None
                    if action.parent_id:
                        parent = processed_actions.get(action.parent_id, None)

                    new_issue = jira_handler.create_issue(action,
                                                          rendered_summary,
                                                          rendered_description,
                                                          rendered_assignee,
                                                          parent,
                                                          group=config.group)

                    processed_actions[action.id] = new_issue
                    created_action_ids.append(action.id)

                    new_issues.append(new_issue)
                    ctx.logger.info(f"New issue {new_issue.id} created")

                # Or there is exactly one new issue (already created or re-used old issue).
                elif len(new_issues) == 1:
                    new_issue = new_issues[0]
                    processed_actions[action.id] = new_issue

                    # If the old issue was reused, re-fresh it.
                    parent = processed_actions[action.parent_id] if action.parent_id else None
                    jira_handler.refresh_issue(action, new_issue)
                    ctx.logger.info(f"Issue {new_issue} re-used")

                # But if there are more than one new issues we encountered error.
                else:
                    raise Exception(f"More than one new {action.id} found ({new_issues})!")

                if action.job_recipe:
                    jira_job = JiraJob(event=artifact_job.event,
                                       erratum=artifact_job.erratum,
                                       compose=artifact_job.compose,
                                       jira=new_issue,
                                       recipe=Recipe(url=action.job_recipe))
                    ctx.save_jira_job('jira-', jira_job)

                # Processing old issues - we only expect old issues that are to be closed (if any).
                if old_issues:
                    if action.on_respin != OnRespinAction.CLOSE:
                        raise Exception(
                            f"Invalid respin action {action.on_respin} for {old_issues}!")
                    for old_issue in old_issues:
                        jira_handler.drop_obsoleted_issue(
                            old_issue, obsoleted_by=processed_actions[action.id])
                        ctx.logger.info(f"Old issue {old_issue} closed")

        # when there is no issue_config we will create one
        # using --issue and --job_recipe parameters
        else:
            if not job_recipe:
                raise Exception("Option --job-recipe is mandatory when --issue-config is not set")
            if issue:
                # verify that specified Jira issue truly exists
                jira_connection = jira.JIRA(jira_url, token_auth=jira_token)
                jira_connection.issue(issue)
                ctx.logger.info(f"Using issue {issue}")
                new_issue = Issue(issue)
            else:
                # when --issue is not specified, we would use an empty string as ID
                # so we will skip Jira reporting steps in later stages
                new_issue = Issue(JIRA_NONE_ID)

            jira_job = JiraJob(event=artifact_job.event,
                               erratum=artifact_job.erratum,
                               compose=artifact_job.compose,
                               jira=new_issue,
                               recipe=Recipe(url=job_recipe))
            ctx.save_jira_job('jira-', jira_job)


@main.command(name='schedule')
@click.option('--arch',
              default=[],
              multiple=True,
              help=('Restrics system architectures to use when scheduling. '
                    'Can be specified multiple times. Example: --arch x86_64'),
              )
@click.pass_obj
def cmd_schedule(ctx: CLIContext, arch: list[str]) -> None:
    ctx.enter_command('schedule')

    for jira_job in ctx.load_jira_jobs('jira-'):
        # prepare parameters based on the recipe from recipe.url
        # generate all relevant test request using the recipe data
        # prepare a list of Request objects

        # would it be OK not to pass compose to TF? I guess so
        compose = jira_job.compose.id if jira_job.compose else None
        if arch:
            architectures = Arch.architectures(
                [Arch(a.strip()) for a in arch])
        else:
            architectures = jira_job.erratum.archs if (
                jira_job.erratum and jira_job.erratum.archs) else Arch.architectures()
        initial_config = RawRecipeConfigDimension(compose=compose,
                                                  environment=ctx.cli_environment,
                                                  context=ctx.cli_context)

        if re.search('^https?://', jira_job.recipe.url):
            config = RecipeConfig.from_yaml_url(jira_job.recipe.url)
        else:
            config = RecipeConfig.from_yaml_file(Path(jira_job.recipe.url))
        # extend dimensions with system architecture but do not override existing settings
        if 'arch' not in config.dimensions:
            config.dimensions['arch'] = []
            for architecture in architectures:
                config.dimensions['arch'].append({'arch': architecture})
        # if RP launch name is not specified in the recipe, set it based on the recipe filename
        if not config.fixtures.get('reportportal', None):
            config.fixtures['reportportal'] = RawRecipeReportPortalConfigDimension()
        # Populate default for config.fixtures['reportportal']['launch_name']
        # Although config.fixtures['reportportal'] is not None, though linter still complaints
        # so we repeat the condition once more
        if ((config.fixtures['reportportal'] is not None) and
                (not config.fixtures['reportportal'].get('launch_name', None))):
            config.fixtures['reportportal']['launch_name'] = os.path.splitext(
                get_url_basename(jira_job.recipe.url))[0]
        # build requests
        jinja_vars = {
            'ERRATUM': jira_job.erratum,
            }

        requests = list(config.build_requests(initial_config, jinja_vars))
        ctx.logger.info(f'{len(requests)} requests have been generated')

        # create ScheduleJob object for each request
        for request in requests:
            # before yaml export render all fields as Jinja templates
            for attr in (
                    "reportportal",
                    "tmt",
                    "testingfarm",
                    "environment",
                    "context",
                    "compose"):
                # compose value is a string, not dict
                if attr == 'compose':
                    value = getattr(request, attr, '')
                    new_value = render_template(
                        value,
                        ERRATUM=jira_job.erratum,
                        COMPOSE=jira_job.compose,
                        CONTEXT=request.context,
                        ENVIRONMENT=request.environment,
                        )
                    if new_value:
                        setattr(request, attr, new_value)
                else:
                    # getattr(request, attr) could also be None due to 'attr' being None
                    mapping = getattr(request, attr, {}) or {}
                    for (key, value) in mapping.items():
                        mapping[key] = render_template(
                            value,
                            ERRATUM=jira_job.erratum,
                            COMPOSE=jira_job.compose,
                            CONTEXT=request.context,
                            ENVIRONMENT=request.environment,
                            )

            # export schedule_job yaml
            schedule_job = ScheduleJob(
                event=jira_job.event,
                erratum=jira_job.erratum,
                compose=jira_job.compose,
                jira=jira_job.jira,
                recipe=jira_job.recipe,
                request=request)
            ctx.save_schedule_job('schedule-', schedule_job)


@main.command(name='execute')
@click.option(
    '--workers',
    default=8,
    help='Limits the number of requests executed in parallel.',
    )
@click.option(
    '--continue',
    '_continue',
    is_flag=True,
    default=False,
    help='Continue with the previous execution, expects --state-dir usage.',
    )
@click.pass_obj
def cmd_execute(ctx: CLIContext, workers: int, _continue: bool) -> None:
    ctx.enter_command('execute')
    ctx.continue_execution = _continue

    # store timestamp of this execution
    ctx.timestamp = str(datetime.datetime.now(datetime.timezone.utc).timestamp())
    tf_token = ctx.settings.tf_token
    if not tf_token:
        raise ValueError("TESTING_FARM_API_TOKEN not set!")
    # make TESTING_FARM_API_TOKEN available to workers as envvar if it has been
    # defined only though the settings file
    os.environ["TESTING_FARM_API_TOKEN"] = tf_token

    # get a list of files to be scheduled so that they can be distributed across workers
    schedule_list = [
        (ctx, ctx.state_dirpath / child.name)
        for child in ctx.state_dirpath.iterdir()
        if child.name.startswith('schedule-')]

    worker_pool = multiprocessing.Pool(workers)
    for _ in worker_pool.starmap(worker, schedule_list):
        # small sleep to avoid race conditions inside tmt code
        time.sleep(0.1)

    print('Done')


def worker(ctx: CLIContext, schedule_file: Path) -> None:

    # modify log message so it contains name of the processed file
    # so that we can distinguish individual workers
    log = partial(lambda msg: ctx.logger.info("%s: %s", schedule_file.name, msg))

    log('processing request...')
    # read request details
    schedule_job = ScheduleJob.from_yaml_file(Path(schedule_file))

    start_new_request = True
    skip_initial_sleep = False
    # if --continue, then read ExecuteJob details as well
    if ctx.continue_execution:
        parent = schedule_file.parent
        name = schedule_file.name
        execute_job_file = Path(os.path.join(parent, name.replace('schedule-', 'execute-', 1)))
        if execute_job_file.exists():
            execute_job = ExecuteJob.from_yaml_file(execute_job_file)
            tf_request = TFRequest(api=execute_job.execution.request_api,
                                   uuid=execute_job.execution.request_uuid)
            start_new_request = False
            skip_initial_sleep = True

    if start_new_request:
        log('initiating TF request')
        tf_request = schedule_job.request.initiate_tf_request(ctx)
        log(f'TF request filed with uuid {tf_request.uuid}')

        # generate Tf command so we can log it
        command_args, environment = schedule_job.request.generate_tf_exec_command(ctx)
        command = ' '.join(command_args)
        # hide tokens
        command = command.replace(ctx.settings.rp_token, '***')
        # export Execution to YAML so that we can report it even later
        # we won't report 'return_code' since it is not known yet
        # This is something to be implemented later
        execute_job = ExecuteJob(
            event=schedule_job.event,
            erratum=schedule_job.erratum,
            compose=schedule_job.compose,
            jira=schedule_job.jira,
            recipe=schedule_job.recipe,
            request=schedule_job.request,
            execution=Execution(request_uuid=tf_request.uuid,
                                request_api=tf_request.api,
                                batch_id=schedule_job.request.get_hash(ctx.timestamp),
                                command=command),
            )
        ctx.save_execute_job('execute-', execute_job)

    # wait for TF job to finish
    finished = False
    delay = int(ctx.settings.tf_recheck_delay)
    while not finished:
        if not skip_initial_sleep:
            time.sleep(delay)
        skip_initial_sleep = False
        tf_request.fetch_details()
        if tf_request.details:
            state = tf_request.details['state']
            envs = ','.join([f"{e['os']['compose']}/{e['arch']}"
                             for e in tf_request.details['environments_requested']])
            log(f'TF request {tf_request.uuid} envs: {envs} state: {state}')
            finished = state in ['complete', 'error']
        else:
            log(f'Could not read details of TF request {tf_request.uuid}')

    # this is to silence the linter, this cannot happen as the former loop cannot
    # finish without knowing request details
    if not tf_request.details:
        raise Exception(f"Failed to read details of TF request {tf_request.uuid}")
    log(f'finished with result: {tf_request.details["result"]["overall"]}')
    # now write execution details once more
    # FIXME: we pretend return_code to be 0
    execute_job.execution.artifacts_url = tf_request.details['run']['artifacts']
    execute_job.execution.return_code = 0
    ctx.save_execute_job('execute-', execute_job)


@main.command(name='report')
@click.option(
    '--rp-project',
    default='',
    help='Overrides ReportPortal project name.',
    )
@click.pass_obj
@click.option(
    '--rp-url',
    default='',
    help='Overrides ReportPortal URL.',
    )
def cmd_report(ctx: CLIContext, rp_project: str, rp_url: str) -> None:
    ctx.enter_command('report')

    jira_request_mapping: dict[str, dict[str, list[str]]] = {}
    jira_launch_mapping: dict[str, RawRecipeReportPortalConfigDimension] = {}
    # jira_group_mapping will store comment restrictions only when defined
    jira_group_mapping: dict[str, str] = {}
    if not rp_project:
        rp_project = ctx.settings.rp_project
    if not rp_url:
        rp_url = ctx.settings.rp_url
    rp = ReportPortal(url=rp_url,
                      token=ctx.settings.rp_token,
                      project=rp_project)
    # initialize Jira connection as well
    jira_url = ctx.settings.jira_url
    if not jira_url:
        raise Exception('Jira URL is not configured!')
    jira_token = ctx.settings.jira_token
    if not jira_token:
        raise Exception('Jira token is not configured!')

    # process each stored execute file
    for execute_job in ctx.load_execute_jobs('execute-'):
        # in record_id we combine short_id and jira_id make it more 'unique'
        # as using jira_id only may not be sufficient
        # especially when not processing --issue-config file
        # we use (artifacts_job) short_id as this is what defines the "granularity"
        # prior the execution of 'jira' subcommand
        short_id = execute_job.short_id
        jira_id = execute_job.jira.id
        record_id = f'{short_id}%{jira_id}'
        if execute_job.jira.group:
            jira_group_mapping[record_id] = execute_job.jira.group
        request_id = execute_job.request.id
        # it is sufficient to process each record_id only once
        if record_id not in jira_request_mapping:
            jira_request_mapping[record_id] = {}
            jira_launch_mapping[record_id] = RawRecipeReportPortalConfigDimension(
                launch_name=execute_job.request.reportportal['launch_name'],
                launch_description=execute_job.request.reportportal.get(
                    'launch_description', None))
        # for each Jira and request ID we build a list of RP launches
        jira_request_mapping[record_id][request_id] = rp.find_launches_by_attr(
            'newa_batch', execute_job.execution.batch_id)

    # proceed with RP launch merge
    for record_id in jira_request_mapping:
        launch_list = []
        jira_id = record_id.split('%')[-1]
        # prepare launch description
        # start with description specified in the recipe file
        description = jira_launch_mapping[record_id].get('launch_description', None)
        if description:
            description += '<br><br>'
        else:
            description = ''
        # add info about the number of recipies scheduled and completed
        description += f'{jira_id}: {len(jira_request_mapping[record_id])} requests in total<br>'
        for request in sorted(jira_request_mapping[record_id].keys()):
            if len(jira_request_mapping[record_id][request]):
                description += f'  {request}: COMPLETED<br>'
                launch_list.extend(jira_request_mapping[record_id][request])
            else:
                description += f'  {request}: MISSING<br>'
        # prepare launch name
        if jira_launch_mapping[record_id]['launch_name']:
            name = str(jira_launch_mapping[record_id]['launch_name'])
        else:
            # should not happen
            name = 'unspecified_newa_launch_name'
        if not len(launch_list):
            ctx.logger.error('Failed to find any related ReportPortal launches')
        else:
            if len(launch_list) > 1:
                merged_launch = rp.merge_launches(
                    launch_list, name, description, {})
                if not merged_launch:
                    ctx.logger.error('Failed to merge ReportPortal launches')
                else:
                    launch_list = [merged_launch]
            # report results back to Jira
            launch_urls = [rp.get_launch_url(str(launch)) for launch in launch_list]
            ctx.logger.info(f'RP launch urls: {" ".join(launch_urls)}')
            # do not report to Jira if JIRA_NONE_ID was used
            if jira_id != JIRA_NONE_ID:
                jira_connection = jira.JIRA(jira_url, token_auth=jira_token)
                try:
                    joined_urls = '\n'.join(launch_urls)
                    description = description.replace('<br>', '\n')
                    jira_connection.add_comment(
                        jira_id,
                        f"NEWA has imported test results to\n{joined_urls}\n\n{description}",
                        visibility={
                            'type': 'group',
                            'value': jira_group_mapping[record_id]}
                        if record_id in jira_group_mapping else None)
                    ctx.logger.info(
                        f'Jira issue {jira_id} was updated with a RP launch URL')
                except jira.JIRAError as e:
                    raise Exception(f"Unable to add a comment to issue {jira_id}!") from e
