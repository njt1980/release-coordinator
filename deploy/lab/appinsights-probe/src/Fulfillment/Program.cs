// Fulfillment: Azure Monitor OpenTelemetry Distro, Application Insights resource A.
// Consumes the Service Bus queue orders (the message's trace context makes the
// consumer span a child of orders' send) and calls inventory over HTTP.
using Azure.Messaging.ServiceBus;
using Azure.Monitor.OpenTelemetry.AspNetCore;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddOpenTelemetry().UseAzureMonitor();
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
