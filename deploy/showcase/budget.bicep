// Monthly cost alert on the subscription, sent to its Owners (no address needed).
targetScope = 'subscription'

param amount int = 25
param startDate string // first day of the current month, yyyy-MM-01

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'rc-showcase-monthly'
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: { startDate: startDate }
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactRoles: ['Owner']
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactRoles: ['Owner']
      }
    }
  }
}
