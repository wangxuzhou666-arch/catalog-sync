"""The ``catalog-sync init`` wizard: from "which service" to a working config file.

``prompts``  asking a question: at the terminal, or from an answers file, through one code path.
``steps``    the questions about the connector, the settings, the compliance block, the login.
``preview``  the normalised sample as a small table, redacted, sensitive fields left out.
``sinks``    where the records go, and the settings of each kind of sink.
``tomlwrite`` the config file, written without a dependency.
``flow``     the order of it all, and the audit line.

The wizard writes the config last, after the login and a sample pull have shown that it works, and
it writes no secret: tokens and passwords are environment variables that the file only names.
"""
