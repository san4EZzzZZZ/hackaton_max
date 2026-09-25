import { useState } from 'react'
import OnboardingScreen from './screens/OnboardingScreen.jsx'
import RouteSetupScreen from './screens/RouteSetupScreen.jsx'
import RouteResultScreen from './screens/RouteResultScreen.jsx'

export default function App() {
  const [screen, setScreen] = useState('onboarding')
  const [settings, setSettings] = useState(null)
  const [route, setRoute] = useState(null)

  const handleRouteReady = (nextSettings, nextRoute) => {
    setSettings(nextSettings)
    setRoute(nextRoute)
    setScreen('result')
  }

  if (screen === 'setup') {
    return (
      <RouteSetupScreen
        initial={settings}
        onBack={() => setScreen('onboarding')}
        onSubmit={(next) => handleRouteReady(next, next.route)}
      />
    )
  }

  if (screen === 'result' && route) {
    return (
      <RouteResultScreen
        route={route}
        onEdit={() => setScreen('setup')}
        onRestart={() => setScreen('onboarding')}
      />
    )
  }

  return <OnboardingScreen onStart={() => setScreen('setup')} />
}
