# Contributing to CONTRIBUTING.md

First off, thanks for taking the time to contribute! ❤️

All types of contributions are encouraged and valued.
See this document for different ways to help and details about how this project handles them. Please make sure to read the relevant section before making your contribution.
It will make it a lot easier for us maintainers and smooth out the experience for all involved.
The community looks forward to your contributions. 🎉

> And if you like the project, but just don't have time to contribute, that's fine. There are other easy ways to support the project and show your appreciation, which we would also be very happy about:
>
> - Star the project
> - Share it on social media
> - Refer this project in your project's README
> - Mention the project in your work/research

## Code of Conduct

This code of conduct applies to on-topic development channels of the project.
This includes but is not limited to: bug trackers, development repositories, mailing lists/discussion forums, and any other communication method for development of software.
Off-topic channels are subject to their own rules and guidelines.

**Standards of Communication**: We expect all users to stay on-topic while using development channels. We will not accept the following: stalking and witchhunting, arguments/off-topic debates, ad hominems, attempts to flame or otherwise derail communication, troll feeding.

Above everything: **Be kind.**

By participating, you are expected to uphold this code. Please report unacceptable behavior
to [daniel.nuest@tu-dresden.de](mailto:daniel.nuest@tu-dresden.de).

## I Have a Question

Before you ask a question, it is best to search for existing [Issues](/issues) that might help you.
In case you have found a suitable issue and still need clarification, you can write your question in this issue.

If you then still feel the need to ask a question and need clarification, we recommend the following:

- Open an [Issue](/issues/new).
- Provide as much context as you can about what you're running into.
- Provide project and platform versions (nodejs, npm, etc), depending on what seems relevant.

We will then take care of the issue as soon as possible.

## I Want To Contribute

**Legal Notice**: When contributing to this project, you must agree that you have authored 100% of the content, that you have the necessary rights to the content and that the content you contribute may be provided under the project license.

### Reporting Bugs

A good bug report shouldn't leave others needing to chase you up for more information. Therefore, we ask you to investigate carefully, collect information and describe the issue in detail in your report. 

Please report security related issues, vulnerabilities or bugs including sensitive information _not_ to the issue tracker, or elsewhere in public.
Instead sensitive bugs must be sent by email to [daniel.nuest@tu-dresden.de](mailto:daniel.nuest@tu-dresden.de) (S/MIME encryption possible).

We use GitHub issues to track bugs and errors. If you run into an issue with the project:

- Open an [Issue](/issues/new).
- Explain the behavior you would expect and the actual behavior.
- Please provide as much context as possible and describe the _reproduction steps_ that someone else can follow to recreate the issue on their own.

### Suggesting Enhancements

Please do!

Enhancement suggestions are tracked as [GitHub issues](/issues).

- Use a **clear and descriptive title** for the issue to identify the suggestion.
- Provide a **step-by-step description of the suggested enhancement** in as many details as possible.
- **Describe the current behavior** and **explain which behavior you expected to see instead** and why. At this point you can also tell which alternatives do not work for you.
- You may want to **include screenshots and animated GIFs** which help you demonstrate the steps or point out the part which the suggestion is related to. You can use [this tool](https://www.cockos.com/licecap/) to record GIFs on macOS and Windows, and [this tool](https://github.com/colinkeenan/silentcast) or [this tool](https://github.com/GNOME/byzanz) on Linux.
- **Explain why this enhancement would be useful** to most CONTRIBUTING.md users. You may also want to point out the other projects that solved it better and which could serve as inspiration.

### Your First Code Contribution

Please open an issue before starting to work on code.
We would like to help you get started!

Here is the gist of it:

- We use a fork & pull development model.
- We use branch names that group and identify the code in the branch, e.g., `feature/the-feature_worked-on` for features, or `bugfix/123` to identify an issue that needs to be fixed.

## Styleguides

### Commit Messages

- Use present tense ("Add feature" not "Added feature").
- Try to make context-aware commit messages. For example, "Fix typo in README" is better than "Fix typo".
- Collect related changes in one commit. For example, "Fix typo in README and add more examples" is better than "Fix typo in README" and "Add more examples".

## Pull requests

### Open a pull request

- Fork the repository and create your branch from `main`.
- Describe all your changes in the `CHANGELOG.md` file in the "[Unreleased]" section.
- Bump the version in `optimap/__init__.py` according to [Semantic Versioning](https://semver.org/).
- Make sure your code is in line with the code formatter and passes all tests.
- Add closing statements to the first comment of the pull request, e.g., `closes #11 #22 #33` to relate the PR to all issues it closes.

### Review a pull request

- All new features should be covered by unit tests
- All tests should pass

## Attribution

This guide is based on the <https://contributing.md> and the [anticode code of conduct](https://jamesoswald.dev/posts/anticode/).
