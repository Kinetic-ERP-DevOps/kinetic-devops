// Syntax-only validation for EFx CustomCodeAction C# fragments.
//
// These fragments are statement bodies extracted from an Epicor EFx function's
// directive graph, not full compilation units, and Epicor's own runtime types
// (Db, CallContext, etc.) aren't available outside the server. So this only
// parses each fragment as a C# script (top-level statements, no Main/class
// wrapper required) and reports genuine syntax errors -- it intentionally
// cannot catch semantic/type errors, since that would require the Epicor
// assemblies this tool doesn't have access to.

using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

var results = new List<object>();
var anyErrors = false;

foreach (var path in args)
{
    string text;
    try
    {
        text = File.ReadAllText(path);
    }
    catch (Exception ex)
    {
        results.Add(new { file = path, ok = false, errors = new[] { new { id = "READ_ERROR", message = ex.Message, line = 0, character = 0 } } });
        anyErrors = true;
        continue;
    }

    var options = new CSharpParseOptions(LanguageVersion.Latest, kind: SourceCodeKind.Script);
    var tree = CSharpSyntaxTree.ParseText(text, options, path: path);
    var diagnostics = tree.GetDiagnostics()
        .Where(d => d.Severity == DiagnosticSeverity.Error)
        .Select(d =>
        {
            var pos = d.Location.GetLineSpan().StartLinePosition;
            return new { id = d.Id, message = d.GetMessage(), line = pos.Line + 1, character = pos.Character + 1 };
        })
        .ToArray();

    if (diagnostics.Length > 0)
    {
        anyErrors = true;
    }

    results.Add(new { file = path, ok = diagnostics.Length == 0, errors = diagnostics });
}

Console.WriteLine(JsonSerializer.Serialize(results, new JsonSerializerOptions { WriteIndented = false }));
Environment.Exit(anyErrors ? 1 : 0);
