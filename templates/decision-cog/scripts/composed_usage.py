"""Native usage entry point backed by an explicitly installed composition.

Canonical source: cog-workbench/bridges/composed_usage.py. Opt-in consumers
vendor this file as scripts/composed_usage.py. No binding comes from task input.
"""
import argparse
import json
from pathlib import Path
import sys


def invoke(root, bundle, suite_type=None):
    root = Path(root).resolve()
    # The host is an explicit sibling installation, never a request-supplied
    # command or Python path. This is a local Workbench host adapter.
    host = root.parent / 'cog-workbench'
    sys.path.insert(0, str(host / 'src'))
    from workbench_suite import Suite, digest, package_digest, require
    config = json.loads((root / '.op-composition.json').read_text())
    checksum = config.pop('sha256')
    require(checksum == digest(config), 'Installed composition integrity failure.')
    require(config['host_sha256'] == package_digest(host), 'Workbench host changed; activate composition again.')
    composition = config['composition']
    require(Path(composition['path']).resolve() == root, 'Installed composition belongs to another consumer.')
    suite = (suite_type or Suite)(workspace=root.parent, state=config['state'])
    result = suite.invoke(composition, bundle)
    result['task'] = 'ask-composed'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', '--bundle', dest='request', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        result = invoke(root, json.loads(Path(args.request).read_text()))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        # The Op records this as an invocation failure, never a model result.
        print(json.dumps({'error': str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
