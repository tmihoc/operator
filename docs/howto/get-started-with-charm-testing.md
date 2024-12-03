(get-started-with-charm-testing)=
# Get started with charm testing

Testing charm code is an essential part of charming. Here we will see how to get started with it. We will look at the templates we have available and the frameworks we can use to write good unit, integration, and functional tests.

**What you'll need:**
- knowledge of testing in general
- knowledge of Juju and charms
- knowledge of the Juju models and events, esp. the data involved in a charm's lifecycle (e.g. see [Talking to a workload control flow from A to Z]())

**What you will learn:**
- What are the starting points for adding tests to a charm?
- What do you typically want to test in a charm?
- How can you do that?
  - What can you unit-test?
     - How to effectively use the Harness.
  - What can you only integration-test?
     - What integration testing frameworks are there?
  - Where can you apply functional testing?
- How to automate this in a CI pipeline.

## Charmcraft profiles

The most popular way to set up a charm project is via `charmcraft init`. For example, to get set up for creating a machine charm, run:

```text
charmcraft init --profile=machine
```

This will provide the following files that you'll use when writing your tests (as well as others for building your charm):

```text
.
├── pyproject.toml
├── spread.yaml
├── tests
│   ├── integration
│   │   └── test_charm.py
│   ├── spread
│   │   ├── general
│   │   │   └── integration
│   │   │       └── task.yaml
│   │   └── lib
│   │       ├── cloud-config.yaml
│   │       └── test-helpers.sh
│   └── unit
│       └── test_charm.py
└── tox.ini
```

There are also profiles for `kubernetes` and for building charms for apps developed with popular frameworks such as Django and Flask.

> See more: [ Write your first Kubernetes charm for a Flask app](https://juju.is/docs/sdk/write-your-first-kubernetes-charm-for-a-flask-app)

## Unit testing

### A charm as an input -> output function

In production, a charm is an object that comes to life when the Juju agent decides to execute it with a given context (we call that an `event`). 
The "inputs" of a charm run are therefore:

 - the event context
 - charm configuration
 - integration (relation) data
 - stored state

Only the event context is guaranteed to be present. The other input sources are optional, but typically a charm will have at least some config and a few integrations adding to its inputs.

The charm code executes and typically produces side-effects aimed at its workload (for example: it writes files to a disk, runs commands on a system, or reconfigures a process) or at other charms it integrates with (for example: it writes relation data). We call this 'operating' a workload, and that is what a charm is meant to do. The ways in which a charm operates can be roughly categorised as:
 
- system operations (e.g. kill a process, restart a service, write a file, emit to a syslog server, make a HTTP request)
- cloud operations (e.g. deploy a Kubernetes service, launch a VM)
- workload operations (e.g. send a request to a local server, write a config file)
- Juju operations (write relation data)

If the charm is a machine charm, workload operation calls can be done directly, while if we're talking about a Kubernetes charm, they will likely be mediated by [Pebble](https://github.com/canonical/pebble).

"Juju operations" are the most 'meta' of them all: they do not affect the workload in and of itself, but they share data which is meant to affect the operation of *other* charms that this charm is integrated with.

> See more: [Talking to a workload: control flow from A to Z](), [Charm lifecycle]() 

### What we are testing when we unit-test

A 'live', deployed Juju application will have access to all the inputs we discussed above, including environment variables, host system access, and more. Unit tests will typically want to mock all that and focus on mapping inputs to expected outputs. Any combination of the input sources we mentioned above can result in any combination of operations. A few examples of increasing complexity of scenarios we may want to unit test:

 - if this event occurs, assert that the charm emits that system call
 - if this event occurs, given this config, assert that the charm writes to the filesystem a config file with this expected content
 - if this event occurs, given this relation data and that config value, assert that that system call is made and this relation data is written (to another relation)
 
You will notice that the starting point is typically always an event. A charm doesn't do anything unless it's being run, and it is only run when an event occurs. So there is *always* an event context to be mocked. This has important consequences for the unit-testing framework, as we will see below.

### The harness

In the charming world, unit testing means using Harness.

> See more [`ops.testing.Harness`](https://ops.readthedocs.io/en/latest/harness.html#ops.testing.Harness)

Harness is the 'mocker' for most inputs and outputs you will need. Where a live charm would gather its input through context variables and calls to the Juju api (by running the hook tools), a charm under unit test will gather data via a mocked backend managed by Harness. Where a live charm would produce output by writing files to a filesystem, Harness exposes a mock filesystem the charm will be able to interact with without knowing the difference. More specific outputs, however, will need to be mocked individually.

A typical test with Harness will look like this:
 
- set things up:
  - set up the charm and its metadata
  - set up the harness
  - mock any 'output' callable that you know would misfire or break (e.g. a system call -- you don't want a unittest to reboot your laptop)
  - configure the charm
  - mock any relation data
 - **mock an event**
 - get the output
 - run assertions on the output

> Obviously, other flows are possible; for example, where you unit test individual charm methods without going through the whole event context setup, but this is the characteristic one.

### Understanding Harness

When you instantiate a `Harness` object, the charm instance does not exist yet. Just like in a live charm, it is possible that when the charm is executed for the first time, the Juju model already has given it storage, relations, some config, or leadership. This delay is meant to give us a chance to simulate this in our test setup. You create a `Harness` object, then you prepare the 'initial state' of the model mock, then you finally initialise the charm and simulate one or more events.

There are two ways to initialize a harnessed charm:

 * When a charm is deployed, it goes through the Setup phase, a fixed sequence of events. `Harness` has a method, `begin_with_initial_hooks()`, that runs this sequence.
 * Alternatively, you can initialise the charm by calling `begin()`. This will instantiate the charm without firing any Setup phase event.

> See more: [A charm's life](), [`ops.testing.Harness.begin_with_initial_hooks()`](https://ops.readthedocs.io/en/latest/harness.html#ops.testing.Harness.begin_with_initial_hooks), [`ops.testing.Harness.begin()`](https://ops.readthedocs.io/en/latest/harness.html#ops.testing.Harness.begin)

After the Setup phase, the charm goes into Operation. To test operation-phase-related events, the harness provides some methods to simulate the most common scenarios. For example: 
 
 - the cloud admin changes the charm config: `harness.update_config`
 - the cloud admin relates this charm to some other: `harness.add_relation`
 - a remote unit joins in a relation (e.g. because the cloud admin has scaled up a remote charm): `harness.add_relation_unit`
 - a remote unit touches its relation data: `harness.update_relation_data`
 - the cloud admin removes a relation: `harness.remove_relation`
 - a resource is attached/detached: `harness.attach_storage` / `harness.detach_storage`
 - a container becomes ready: `harness.container_pebble_ready`

Therefore, one typically will not have to manually `.emit()` events, but can rely on the `Harness` utilities and focus on the higher level abstractions that they expose.

### Writing a test

The typical way in which we want to structure a test is:
 - configure the required inputs
 - mock any function or system call you need to
 - initialise the charm
 - fire some event OR use one of the harness methods to trigger a predefined event sequence
 - assert some output matches what is expected, or some function is called with the expected parameters, etc...

A simple test might look like this:

```python
from charm import MyCharm
from ops import testing

def test_pebble_ready_writes_config_file():
    """Test that on pebble-ready, a config file is written"""
    harness: testing.Harness[MyCharm] = testing.Harness(MyCharm)
    # If you want to mock charm config:
    harness.update_config({'foo': 'bar'})
    # If you want to mock charm leadership:
    harness.set_leader(True)

    # If you want to mock relation data:
    relation_ID = harness.add_relation('relation-name', 'remote-app-name')
    harness.add_relation_unit(relation_ID, 'remote-app-name/0')
    harness.update_relation_data(relation_ID, 'remote-app-name/0', {'baz': 'qux'})
    
    # We are done setting up the inputs.

    harness.begin()
    charm = harness.charm  # This is a MyCharm instance.
    
    # This will fire a `<container-name>-pebble-ready` event.
    harness.container_pebble_ready("workload")

    # Suppose that MyCharm has written a YAML config file via Pebble.push():
    container = charm.unit.get_container("workload")
    file = "/opt/workload/path_to_config_file.yaml"
    config = yaml.safe_load(container.pull(file).read())
    assert config[0]['foo']['bar'] == 'baz'  # or whatever
``` 

```{note}

An important difference between a harnessed charm and a 'live', deployed, charm is that `Harness` holds on to the charm instance between events, while a deployed charm garbage-collects the charm instance between hooks. So if your charm were to set some states in, say, instance attributes, and rely on it on subsequent event handling loops, the unit tests based on the harness would not be able to catch that mistake. An integration test would.

```

## Integration testing

Where unit testing focuses on black-box method-by-method verification, integration testing focuses on the big picture. Typically integration tests check that the charm does not break (generally this means: blocks with status `blocked` or `error`) when a (mocked) cloud admin performs certain operations. These operations are scripted by using, in order of abstraction:
 -  shell commands against [the `juju` cli](https://juju.is/docs/olm/juju-cli-commands)
 - [`python-libjuju`](https://github.com/juju/python-libjuju), wrapping juju api calls
   - [`pytest-operator`](https://github.com/charmed-kubernetes/pytest-operator), a `pytest` plugin wrapping `python-libjuju`
   - [`zaza`](https://zaza.readthedocs.io/en/latest/index.html), a testing-framework-agnostic wrapper on top of `python-libjuju` 

Things you typically want to test with integration tests:
 - The charm can be packed (i.e. `charmcraft pack` does not error)
 - The charm can be deployed (i.e. `juju deploy ./packed_charm.charm` deploys an application that reaches `active` or `waiting` within a reasonable time frame)

These are 'smoke tests' that should always be present, and are provided for you when using `charmcraft init`. The following are non-smokey, proper integration tests.
- The charm can be related to other applications without erroring
  - and the relation has the expected effect on the charm's operation logic
- The charm can be configured
  - and the config has the expected effect on the charm's operation logic
- The actions supported by the charm can be executed
  - and return the expected results
- Given any combination of the above relations, configs, etc, the charm 'works': the workload it operates does whatever it is supposed to do.

### Testing with `pytest-operator`

The integration test template that `charmcraft init` provides includes the starting point for writing integration tests with `pytest-operator` and `python-libjuju`. The `tox.ini` is also configured so that you can run `tox -e integration` to run the tests, provided that you have a cloud (such as LXD or microk8s) and local Juju client.

The entry point for all `pytest-operator` tests is the `ops_test` fixture. The fixture is a module-scoped context which, on entry, adds to Juju a randomly-named new model and destroys it on exit. All tests in that module, and all interactions with the `ops_test` object, will take place against that model.

Once you have used `ops_test` to get a model in which to run your integration tests, most of the remaining integration test code will interact with Juju via the `python-libjuju` package.

> See more: [`python-libjuju`](https://pythonlibjuju.readthedocs.io/en/latest/)

```{note}

*Pro tip*: you can prevent `ops_test` from tearing down the model on exit by passing the `--keep-models` argument. This is useful when the tests fail and the logs don't provide a sufficient post-mortem and a real live autopsy is required.

```

Detailed documentation of how to use `ops_test` and `pytest-operator` is out of scope for this document. However, this is an example of a typical integration test:

```python
async def test_operation(ops_test: OpsTest):
    # Tweak the config:
    app: Application = ops_test.model.applications.get("tester")
    await app.set_config({"my-key": "my-value"})
    
    # Add another charm and relate them:
    await ops_test.model.deploy('other-app')
    await ops_test.model.relate('tester:endpoint1', 'other-charm:endpoint2')
    
    # Scale it up:
    await app.add_unit(2)
    
    # Remove another app:
    await ops_test.model.remove_application('yet-another-app')
    
    # Run an action on a unit:
    unit: Unit = app.units[1]
    action = await unit.run('my-action')
    assert action.results == <foo>
    
    # What this means depends on the workload:
    assert charm_operates_correctly()  
```

`python-libjuju` has, of course, an API for all inverse operations: remove an app, scale it down, remove a relation...

A good integration testing suite will check that the charm continues to operate as expected whenever possible, by combining these simple elements.

## Functional testing

Some charms represent their workload by means of an object-oriented wrapper, which mediates between operator code and the implementation of operation logic. In such cases, it can be useful to add a third category of tests, namely functional tests, that black-box test that workload wrapper without worrying about the substrate it runs on (the charm, the cloud, the machine or pod...).
For an example charm adopting this strategy, see [parca-operator](https://github.com/jnsgruk/parca-operator). Nowadays, the preferred tool to do functional testing is Scenario.

> See more: [Scenario](https://github.com/canonical/ops-scenario), {ref}`Write a Scenario test for a charm <write-scenario-tests-for-a-charm>`

## Continuous integration

Typically, you want the tests to be run automatically against any PR into your repository's main branch, and sometimes, to trigger a new release whenever that succeeds. CD is out of scope for this article, but we will look at how to set up a basic CI.

Create a file called `.github/workflows/ci.yaml`. For example, to include a `lint` job that runs the `tox` `lint` environment:

```yaml
name: Tests
on:
  workflow_call:

jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v3
      - name: Install dependencies
        run: python3 -m pip install tox
      - name: Run linters
        run: tox -e lint
```

Other `tox` environments can be run similarly; for example unit tests:

```yaml
  unit-test:
    name: Unit tests
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v3
      - name: Install dependencies
        run: python -m pip install tox
      - name: Run tests
        run: tox -e unit
```

Integration tests are a bit more complex, because in order to run those tests, a Juju controller and a cloud in which to deploy it, is required. This example uses a `actions-operator` workflow provided by `charmed-kubernetes` in order to set up `microk8s` and Juju:

```
  integration-test-microk8s:
    name: Integration tests (microk8s)
    needs:
      - lint
      - unit-test
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v3
      - name: Setup operator environment
        uses: charmed-kubernetes/actions-operator@main
        with:
          provider: microk8s
      - name: Run integration tests
        # Set a predictable model name so it can be consumed by charm-logdump-action
        run: tox -e integration -- --model testing
      - name: Dump logs
        uses: canonical/charm-logdump-action@main
        if: failure()
        with:
          app: my-app-name
          model: testing
```

> You can find more actions, advanced documentation and use cases in [charming-actions](https://github.com/canonical/charming-actions)

## Conclusion

We have examined all angles one might take when testing a charm, and given a brief overview of the most popular frameworks for implementing unit and integration tests, all the way to how one would link them up with a CI system to make sure the repository remains clean and tested.
