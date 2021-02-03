# Dagster Documentation Site

This folder contains the code for the Dagster documentation site at https://docs.dagster.io

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

## Editing API documentation

The API documentation is authored separately in the `dagster/docs/sphinx` folder using [Sphinx](https://www.sphinx-doc.org/en/master/), a Python document generator. We use Sphinx because it has built-in functionality to document Python methods and classes by parsing the docstrings.

If you update the API documentation in the `dagster/docs/sphinx` folder, you need to rebuild the output from Sphinx in order to see your changes reflected in the documentation site.

In the `dagster/docs` directory (not this directory), run:

```
make build
```

If you don't build the API documentation and include the changes in your diff, you will see a build error reminding you to do so.

## Writing MDX

We use MDX, which is a content format that lets use use JSX in our markdown documents. This lets us import components and embed them in our documentation.

### Using Components

The components available to use in MDX are listed in the `/components/MDXComponents` file.

### PyObject Component

The most important component is the `<PyObject />` component. It is used to link to references in the API documentation from MDX files.

Example usage:

```
Here is an example of using PyObject: <PyObject module="dagster" object="SolidDefinition" />
By default, we just display the object name. To override, use the `displayText` prop: <PyObject module="dagster" object="solid" displayText="@solid"/ >
```

### Code Snippets (Literal Includes)

We want the code snippets in our documentation to be high quality. We maintain quality by authoring snippets in the `/examples/docs_snippets` folder and testing them to make sure they work and we don't break them in the future.

To include snippets from the code base, you can provide additional properties to the markdown code fence block:

Using markers:

    ```python file=/overview/solids_pipelines/solid_definition.py startafter=start_solid_definition_marker_0 endbefore=end_solid_definition_marker_0
    ```

Using lines (highly discouraged):

    ```python file=/overview/solids_pipelines/solid_definition.py lines=3-10
    ```

Properties:

- `file`: The path to file relative to the `/examples/docs_snippets/docs_snippets` folder. You can use a relative path from this folder to access snippets from other parts of the codebase, but this is _highly discouraged_. Instead, you should copy the bit of code you need into `doc_snippets` and test it appropriately.
- `startafter` and `endbefore`: Use this property to specify a code snippet in between to makers. You will need to include the markers in the source file as comments.
- `lines`: Use this property to specify a range of lines to include in the code snippet. You can also pass multiple ranges separated by commas. For example: `lines="1-10,12,14-20"
- `trim=true`: Sometimes, our Python formatter `black` gets in the way and adds some spacing before the end of your snippet and the `endbefore` marker comment. Use trim to get rid of any extra newlines.

To actually get the snipets to render. run `yarn snapshot`. This will replace the body of the code block with the code you referenced.

**Important**:

To change the value of a literal include, you must change the referenced code, not the code inside the code block. Run `yarn snapshot` once you update the underlying code to see the changes in the doc site. This behavior is different from previous versions of the site.

### Navigation

If you are adding a new page or want to update the navigation in the sidebar, update the `docs/next/content/_navigation.json` file.