# Third-party notices

## Runtime

Echem Platform `0.1.4` uses only the Python standard library and browser-native HTML, CSS, Canvas, and JavaScript APIs. The repository does not vendor a third-party Python package, JavaScript library, font, SDK, or instrument driver, and the application does not load code from a CDN at runtime.

Python itself is not bundled in the source release. Users are responsible for obtaining Python under the terms published by the Python Software Foundation.

## Development and CI services

The GitHub Actions workflow references `actions/checkout` and `actions/setup-python`. These actions run in GitHub's CI environment and are not included in the Echem Platform runtime or release archive.

## Future portable packages

A future Windows portable package may bundle Python or other separately licensed components. Such a package must include a generated dependency inventory, applicable license texts, versions, and hashes before publication. NumPy, Pandas, Matplotlib, vendor SDKs, installers, and drivers are not part of the `0.1.4` source release.

If an omitted third-party attribution is found, please open a documentation issue unless the report contains a security vulnerability.
