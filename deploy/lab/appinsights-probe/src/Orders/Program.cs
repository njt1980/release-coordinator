// Orders: classic Application Insights SDK 2.x + Kubernetes enricher,
// Application Insights resource B. GET /place calls inventory (resource A,
// so the classic SDK records "inventory | cid-v1:<appId>"), inserts a row in
// SQL Server ("tcp:mssql,1433 | orders") and sends to the Service Bus queue
// ("sb://<namespace>/ | orders").
using Azure.Messaging.ServiceBus;
using Microsoft.ApplicationInsights.DependencyCollector;
using Microsoft.Data.SqlClient;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddApplicationInsightsTelemetry();
builder.Services.AddApplicationInsightsKubernetesEnricher();
// Azure SDK (Service Bus) activities are only collected when listed.
builder.Services.ConfigureTelemetryModule<DependencyTrackingTelemetryModule>(
    (module, _) => module.IncludeDiagnosticSourceActivities.Add("Azure.Messaging.ServiceBus"));
builder.Services.AddHttpClient("inventory", client =>
    client.BaseAddress = new Uri(builder.Configuration["INVENTORY_URL"] ?? "http://inventory"));
builder.Services.AddSingleton(_ => new ServiceBusClient(builder.Configuration["SERVICEBUS_CONNECTION"]));
builder.Services.AddSingleton(services => services.GetRequiredService<ServiceBusClient>()
    .CreateSender(builder.Configuration["ORDERS_QUEUE"] ?? "orders"));

var connectionString = builder.Configuration.GetConnectionString("Orders")
    ?? throw new InvalidOperationException("ConnectionStrings__Orders is not set");
var app = builder.Build();
await EnsureDatabaseAsync(connectionString, app.Logger);

app.MapGet("/place", async (IHttpClientFactory clients, ServiceBusSender sender) =>
{
    var stock = await clients.CreateClient("inventory").GetStringAsync("/stock/sku-1");
    await using (var connection = new SqlConnection(connectionString))
    {
        await connection.OpenAsync();
        await using var command = new SqlCommand(
            "INSERT INTO Orders (Sku, PlacedAt) VALUES (@sku, SYSUTCDATETIME())", connection);
        command.Parameters.AddWithValue("@sku", "sku-1");
        await command.ExecuteNonQueryAsync();
    }
    await sender.SendMessageAsync(new ServiceBusMessage("sku-1"));
    return Results.Ok(stock);
});
app.Run();

static async Task EnsureDatabaseAsync(string connectionString, ILogger logger)
{
    var master = new SqlConnectionStringBuilder(connectionString) { InitialCatalog = "master" }.ConnectionString;
    for (var attempt = 1; ; attempt++)
    {
        try
        {
            await using (var connection = new SqlConnection(master))
            {
                await connection.OpenAsync();
                await using var create = new SqlCommand(
                    "IF DB_ID('orders') IS NULL CREATE DATABASE orders", connection);
                await create.ExecuteNonQueryAsync();
            }
            await using (var connection = new SqlConnection(connectionString))
            {
                await connection.OpenAsync();
                await using var table = new SqlCommand(
                    "IF OBJECT_ID('Orders') IS NULL CREATE TABLE Orders " +
                    "(Id INT IDENTITY PRIMARY KEY, Sku NVARCHAR(50) NOT NULL, PlacedAt DATETIME2 NOT NULL)",
                    connection);
                await table.ExecuteNonQueryAsync();
            }
            return;
        }
        catch (SqlException error) when (attempt < 30)
        {
            logger.LogWarning("SQL Server not ready (attempt {Attempt}): {Message}", attempt, error.Message);
            await Task.Delay(TimeSpan.FromSeconds(5));
        }
    }
}
