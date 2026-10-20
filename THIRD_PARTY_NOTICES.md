# Third-party notices

The project currently provides no open-source license for its own code and
documentation. This does not change the licenses of third-party components.

The offline Python test environment bundles these unmodified wheels in
`runner_app/git/python_quick_wheels/`. Each wheel contains its original copyright
and license notices in the indicated archive members.

| Component | Version | Declared license | Notice inside the wheel |
| --- | --- | --- | --- |
| iniconfig | 2.3.0 | MIT | `iniconfig-2.3.0.dist-info/licenses/LICENSE` |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | `packaging-26.3.dist-info/licenses/LICENSE`, `LICENSE.APACHE`, `LICENSE.BSD` |
| pluggy | 1.6.0 | MIT | `pluggy-1.6.0.dist-info/licenses/LICENSE` |
| Pygments | 2.21.0 | BSD-2-Clause | `pygments-2.21.0.dist-info/licenses/LICENSE` and `AUTHORS` |
| pytest | 9.1.1 | MIT | `pytest-9.1.1.dist-info/licenses/LICENSE` |

Other dependencies are installed separately from the lock files; their package
metadata and distributions carry their respective licenses. Imported design and
skill sources retain their existing provenance records.
