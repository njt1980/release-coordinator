// Billing: classic Application Insights SDK 2.x + Kubernetes enricher,
// Application Insights resource A. A second consumer of the Service Bus queue
// orders (competing with fulfillment), so the classic SDK's consumer telemetry
// can be checked: with Azure.Messaging.ServiceBus listed, ProcessMessage
// should become a request whose ParentId is the producer's send.
using Azure.Messaging.ServiceBus;
using Microsoft.ApplicationInsights.DependencyCollector;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddApplicationInsightsTelemetry();
builder.Services.AddApplicationInsightsKubernetesEnricher();
builder.Services.ConfigureTelemetryModule<DependencyTrackingTelemetryModule>(
    (module, _) => module.IncludeDiagnosticSourceActivities.Add("Azure.Messaging.ServiceBus"));
builder.Services.AddHttpClient("inventory", client =>
    client.BaseAddress = new Uri(builder.Configuration["INVENTORY_URL"] ?? "http://inventory"));
builder.Services.AddSingleton(_ => new ServiceBusClient(builder.Configuration["SERVICEBUS_CONNECTION"]));
builder.Services.AddHostedService<OrderConsumer>();

var app = builder.Build();
app.Run();

sealed class OrderConsumer(
    ServiceBusClient client, IHttpClientFactory clients, IConfiguration configuration, ILogger<OrderConsumer> logger)
    : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await using var processor = client.CreateProcessor(configuration["ORDERS_QUEUE"] ?? "orders");
        processor.ProcessMessageAsync += async args =>
        {
            await clients.CreateClient("inventory").GetStringAsync($"/stock/{args.Message.Body}", args.CancellationToken);
            await args.CompleteMessageAsync(args.Message, args.CancellationToken);
        };
        processor.ProcessErrorAsync += args =>
        {
            logger.LogWarning(args.Exception, "Service Bus processing failed");
            return Task.CompletedTask;
        };
        await processor.StartProcessingAsync(stoppingToken);
        try { await Task.Delay(Timeout.Infinite, stoppingToken); }
        catch (OperationCanceledException) { }
        await processor.StopProcessingAsync();
    }
}
