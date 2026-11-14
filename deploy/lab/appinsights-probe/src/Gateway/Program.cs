// Gateway: Azure Monitor OpenTelemetry Distro, Application Insights resource A.
// GET /place calls orders over HTTP (a cross-resource call: orders reports to B).
using Azure.Monitor.OpenTelemetry.AspNetCore;

var builder = WebApplication.CreateBuilder(args);
// Reads APPLICATIONINSIGHTS_CONNECTION_STRING; role name from OTEL_SERVICE_NAME.
builder.Services.AddOpenTelemetry().UseAzureMonitor();
builder.Services.AddHttpClient("orders", client =>
    client.BaseAddress = new Uri(builder.Configuration["ORDERS_URL"] ?? "http://orders"));

var app = builder.Build();
app.MapGet("/place", async (IHttpClientFactory clients) =>
{
    using var response = await clients.CreateClient("orders").GetAsync("/place");
    return Results.StatusCode((int)response.StatusCode);
});
app.Run();
