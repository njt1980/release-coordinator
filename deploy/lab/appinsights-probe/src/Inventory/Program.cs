// Inventory: classic Application Insights SDK 2.x + Kubernetes enricher,
// Application Insights resource A. Called by orders (resource B) and fulfillment (A).
var builder = WebApplication.CreateBuilder(args);
// Reads APPLICATIONINSIGHTS_CONNECTION_STRING; the enricher names the role after the container.
builder.Services.AddApplicationInsightsTelemetry();
builder.Services.AddApplicationInsightsKubernetesEnricher();

var app = builder.Build();
app.MapGet("/stock/{sku}", (string sku) => Results.Ok(new { sku, available = Random.Shared.Next(0, 100) }));
app.Run();
