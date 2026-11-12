# Connecting an inventory tool

A walkthrough for the developers of a program that prints labels through
the print server, here an inventory tool that prints asset labels: one
label, a series of labels in one request, following the job, printing
again after a jam, and handling errors. Every request is described in
the [API reference](api.md); setting up the server in
[Server installation](server-install.md).

## The setup { #setup }

The examples use these names; yours will differ:

- The server: `https://labels.example.com/api/v1`, reached through the
  company's reverse proxy.
- The label `asset-tag.tavletta` in the labels folder, designed in the
  Tavletta app, with a QR code and a text that both contain the
  placeholder `{AssetNumber}`.
- The printer `it-cab` of `printers.toml`: a cab label printer with
  thermal transfer (`method = "transfer"`), printing turned by 180
  degrees (`upside_down = true`).
- An access token with the role `print`, limited to `it-cab`, made by the
  server's administrator:

  ```sh
  tavletta token new inventory-tool --role print --printer it-cab \
      --tokens tokens.toml --printers printers.toml
  ```

Keep the token in the tool's secret store (configuration encrypted, a
vault), not in the code. In the shell examples:

```sh
TAVLETTA=https://labels.example.com/api/v1
TOKEN=tav_3f9c0a1b2c4d_Xq2…
```

## 1. What the label needs { #describe }

```sh
curl -H "Authorization: Bearer $TOKEN" "$TAVLETTA/labels/asset-tag"
```

The answer lists the label's `columns` (here `["AssetNumber"]`): every
data row needs these keys. A placeholder without a value in the row
refuses the print with 422 `label_unprintable` ("No such column"), so a
label never prints a half-empty text.

## 2. Preview { #preview }

A preview prints nothing and returns the label as a PNG image, for
example to show it in the tool before printing:

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "asset-tag", "printer": "it-cab", "data_rows": [{"AssetNumber": "100001"}]}' \
     -o preview.png "$TAVLETTA/preview"
```

- With `"format": "json"` the answer is JSON with the image in base64
  and the `warnings` (for example `font_missing`, `barcode_narrow`).
- `"view": "printer"` shows the image as the printer gets it (turned).
- A preview needs only the role `preview`.

## 3. Print one label { #one }

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "asset-tag", "printer": "it-cab", "data_rows": [{"AssetNumber": "100001"}], "wait": 30}' \
     "$TAVLETTA/print"
```

- The server checks the request, queues a job for the printer and
  answers with the job. With `"wait": 30` it waits up to 30 seconds for
  the job: 200 with `"state": "done"` once printed, else 202 with the job
  still `queued` or `printing`.
- The answer's `Location` header and the job's `id` lead to
  `GET /api/v1/jobs/{id}`.
- Each printer prints one job after the other; jobs of different printers
  run side by side.

## 4. A series in one request { #series }

Twenty asset labels are one request with twenty data rows, not twenty
requests: they print as one job, in order, and a problem stops the job at
a known row.

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "asset-tag", "printer": "it-cab", "wait": 60,
          "data_rows": [{"AssetNumber": "100001"}, {"AssetNumber": "100002"},
                        {"AssetNumber": "100003"}, {"AssetNumber": "100020"}]}' \
     "$TAVLETTA/print"
```

The recommended pattern: **the tool owns the numbers.** The tool takes
the next free asset numbers from its own database, saves them with the
assets, and sends them as data rows. The label never counts by itself, so
a second server, a restored backup or a reprint can never hand out a
number twice.

### The alternative: the label's counter { #counter }

A label with the variable `{=counter}` (for example `{=counter:000000}`
for six digits) counts by itself. The tool can still say where to start:

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "asset-tag-counter", "printer": "it-cab",
          "counter_start": 100001, "copies": 20, "no_save_counter": true}' \
     "$TAVLETTA/print"
```

- `counter_start` is the first number; the counter goes up with every
  label and copy: 100001 to 100020.
- `no_save_counter`: the server does not save the next number into the
  label file. Without it, the label file keeps the next number (100021),
  and the next print without `counter_start` goes on from there.
- Use this only when the numbers do not need to be in the tool's
  database before printing.

## 5. Follow the job { #job }

```sh
curl -H "Authorization: Bearer $TOKEN" "$TAVLETTA/jobs/$JOB"
```

| `state` | Meaning |
|---|---|
| `queued` | waits for the printer |
| `waiting` | waits for the label file (another job is saving its counter) |
| `printing` | is printing; `printed` of `total` labels are out |
| `done` | all labels printed |
| `failed` | stopped: `error` says why, `rows_printed` how far it came |
| `canceled` | canceled |

- Ask every one or two seconds while the state is not final; the
  server keeps finished jobs for 24 hours.
- A `print` token sees its own jobs only; another token's job is "not
  found".

## 6. After a jam: print the rest { #reprint }

When the printer runs out of labels or jams, the job fails, for example:

```json
{"state": "failed", "printed": 13, "total": 20, "rows_printed": 13,
 "error": {"code": "printer_out_of_labels",
           "message": "… Printing stopped at row 14. To continue, print again from row 14."}}
```

Once the printer is ready, send the same request again with `rows`, the
rows still missing:

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "asset-tag", "printer": "it-cab", "rows": "14-20",
          "data_rows": [{"AssetNumber": "100001"}, {"AssetNumber": "100020"}]}' \
     "$TAVLETTA/print"
```

`rows` counts the data rows from 1: `"14-20"`, `"14-"` (to the last),
`"5"`. Send the same data rows as before, so row 14 is the same asset.
A label that was half printed when the printer stopped belongs to the
row that failed and is printed again.

## 7. Cancel { #cancel }

```sh
curl -X DELETE -H "Authorization: Bearer $TOKEN" "$TAVLETTA/jobs/$JOB"
```

A queued job is canceled at once; a printing one stops after the label
being printed. 409 `job_finished` if it had finished already.

## 8. Retries and errors { #errors }

`POST /print` is not idempotent: a request sent twice prints twice. So:

- **Retry automatically** only answers that were refused before a job
  was made: 503 `busy` and 429 `queue_full`, after a pause that grows (1,
  2, 4, 8 s).
- **Do not retry automatically** when the connection broke or timed out
  after the request was sent: the job may exist and print. Show the
  operator what happened, let them check the printer, then print the
  missing labels with `rows`.
- **Fix the request** for other 4xx answers; the code says what is wrong:

| Code | What the tool does |
|---|---|
| `unauthorized` (401) | Token wrong, expired or revoked: ask the administrator for a new one. |
| `forbidden` (403) | The token's role is too low. |
| `printer_not_found`, `label_not_found` (404) | Name typed wrong, or the token may not use that printer. |
| `invalid_field`, `unknown_field` (400) | A bug in the tool: the message names the field. |
| `too_many_copies` (400), `too_many_labels` (413) | Split the series into several jobs. |
| `label_unprintable` (422) | The data does not fit the label (a missing column, a code that cannot hold the value): show the message. |
| `busy` (503), `queue_full` (429) | Retry after a pause. |
| `printer_<code>` in a failed job | Show the message to the operator; continue with `rows` once fixed. |

The message (`error.message`) is in the language of `Accept-Language`
and meant for people: show it, but decide by the code.

## Example: Python { #python }

With the `requests` package:

```python
import os
import time

import requests

BASE = os.environ.get("TAVLETTA", "https://labels.example.com/api/v1")
TOKEN = os.environ["TAVLETTA_TOKEN"]  # from the secret store, never in the code
FINAL = {"done", "failed", "canceled"}


class LabelError(Exception):
    def __init__(self, status, code, message):
        super().__init__("%s %s: %s" % (status, code, message))
        self.status, self.code = status, code


session = requests.Session()
session.headers.update({"Authorization": "Bearer " + TOKEN, "Accept-Language": "de-CH"})


def call(method, path, **kwargs):
    response = session.request(method, BASE + path, timeout=330, **kwargs)
    if response.status_code >= 400:
        try:
            error = response.json()["error"]
        except (ValueError, KeyError):  # not from Tavletta, e.g. the proxy
            error = {"code": "http_%d" % response.status_code, "message": response.text}
        raise LabelError(response.status_code, error["code"], error["message"])
    return response


def print_assets(numbers, printer="it-cab", rows=None):
    """One label per asset number, in one job; returns the finished job."""
    body = {
        "label": "asset-tag",
        "printer": printer,
        "data_rows": [{"AssetNumber": str(n)} for n in numbers],
        "wait": 30,
    }
    if rows:
        body["rows"] = rows  # e.g. "14-20": only these rows
    for attempt in range(5):
        try:
            job = call("POST", "/print", json=body).json()
            break
        except LabelError as e:
            # refused before it was queued: nothing printed, safe to retry
            if e.code not in ("busy", "queue_full") or attempt == 4:
                raise
            time.sleep(2**attempt)
    while job["state"] not in FINAL:  # still printing after "wait"
        time.sleep(2)
        job = call("GET", "/jobs/" + job["id"]).json()
    return job


def preview(number, path="preview.png"):
    body = {"label": "asset-tag", "data_rows": [{"AssetNumber": str(number)}]}
    with open(path, "wb") as file:
        file.write(call("POST", "/preview", json=body).content)


if __name__ == "__main__":
    preview(100001)
    numbers = range(100001, 100021)  # 20 labels
    job = print_assets(numbers)
    if job["state"] == "failed":
        print("Stopped:", job["error"]["message"])
        # rows_printed rows are done: print the rest once the printer is fixed
        rest = "%d-%d" % (job["rows_printed"] + 1, len(numbers))
        print("To continue:", rest)
    else:
        print(job["state"], job["printed"], "of", job["total"])
```

## Example: C# and .NET { #csharp }

With `HttpClient` and `System.Net.Http.Json` (.NET 8 or newer), as a
console program; in an application, register one `HttpClient` (for
example with `IHttpClientFactory`) and reuse it.

```csharp
using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

var baseUrl = Environment.GetEnvironmentVariable("TAVLETTA") ?? "https://labels.example.com/api/v1";
var token = Environment.GetEnvironmentVariable("TAVLETTA_TOKEN")!; // from the secret store

using var http = new HttpClient { BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/"), Timeout = TimeSpan.FromSeconds(330) };
http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
http.DefaultRequestHeaders.AcceptLanguage.ParseAdd("de-CH");
var labels = new LabelClient(http);

var numbers = Enumerable.Range(100001, 20).Select(n => n.ToString()).ToList();
var job = await labels.PrintAssetsAsync(numbers, "it-cab");
Console.WriteLine(job.State == "failed"
    ? $"Stopped: {job.Error?.Message} To continue: rows {job.RowsPrinted + 1}-{numbers.Count}"
    : $"{job.State}: {job.Printed} of {job.Total}");

public record ApiError(string Code, string Message);
public record ErrorBody(ApiError Error);
public record Job(
    string Id, string State, int Printed, int Total,
    [property: JsonPropertyName("rows_printed")] int RowsPrinted, ApiError? Error);

public class LabelException(HttpStatusCode status, string code, string message) : Exception(message)
{
    public HttpStatusCode Status { get; } = status;
    public string Code { get; } = code;
}

public class LabelClient(HttpClient http)
{
    static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    public async Task<Job> PrintAssetsAsync(IList<string> assetNumbers, string printer, string? rows = null)
    {
        var body = new Dictionary<string, object>
        {
            ["label"] = "asset-tag",
            ["printer"] = printer,
            ["data_rows"] = assetNumbers.Select(n => new Dictionary<string, string> { ["AssetNumber"] = n }),
            ["wait"] = 30,
        };
        if (rows != null) body["rows"] = rows; // e.g. "14-20"
        Job job;
        for (var attempt = 0; ; attempt++)
        {
            try { job = await SendAsync<Job>(HttpMethod.Post, "print", body); break; }
            catch (LabelException e) when (e.Code is "busy" or "queue_full" && attempt < 4)
            {
                await Task.Delay(TimeSpan.FromSeconds(Math.Pow(2, attempt))); // nothing was printed
            }
        }
        while (job.State is "queued" or "waiting" or "printing")
        {
            await Task.Delay(2000);
            job = await SendAsync<Job>(HttpMethod.Get, $"jobs/{job.Id}");
        }
        return job;
    }

    public Task<Job> CancelAsync(string jobId) => SendAsync<Job>(HttpMethod.Delete, $"jobs/{jobId}");

    async Task<T> SendAsync<T>(HttpMethod method, string path, object? body = null)
    {
        using var request = new HttpRequestMessage(method, path);
        // as a string: the server needs a Content-Length (JsonContent would send it chunked)
        if (body != null)
            request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        using var response = await http.SendAsync(request);
        if (!response.IsSuccessStatusCode)
        {
            ErrorBody? error = null;
            try { error = await response.Content.ReadFromJsonAsync<ErrorBody>(Json); } catch (JsonException) { }
            throw new LabelException(response.StatusCode,
                error?.Error.Code ?? $"http_{(int)response.StatusCode}",
                error?.Error.Message ?? response.ReasonPhrase ?? "");
        }
        return (await response.Content.ReadFromJsonAsync<T>(Json))!;
    }
}
```

- The base address ends with `/` and the paths have none at the start
  (`"print"`, `"jobs/…"`): `HttpClient` then keeps `/api/v1`.
- The body goes as a `StringContent`: the server needs a
  `Content-Length`, and `JsonContent.Create` would send the body in chunks
  without one (411 `length_required`).
