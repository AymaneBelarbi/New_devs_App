import React, { useEffect, useState } from "react";
import { RevenueSummary } from "./RevenueSummary";
import { useAuth } from "../contexts/AuthContext.new";
import { SecureAPI, type DashboardProperty } from "../lib/secureApi";

const Dashboard: React.FC = () => {
  const { user } = useAuth();
  const identity = `${user?.id || ''}:${user?.tenant_id || ''}`;
  const [propertyList, setPropertyList] = useState<{ identity: string; properties: DashboardProperty[] } | null>(null);
  const [selectedProperty, setSelectedProperty] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [period, setPeriod] = useState(() => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  });
  const [year, month] = period.split('-').map(Number);
  const properties = propertyList?.identity === identity ? propertyList.properties : [];

  useEffect(() => {
    let cancelled = false;
    setPropertyList(null);
    setSelectedProperty('');
    setError('');
    setLoading(true);

    const loadProperties = async () => {
      try {
        const authorizedProperties = await SecureAPI.getDashboardProperties();
        if (cancelled) return;
        setPropertyList({ identity, properties: authorizedProperties });
        setSelectedProperty(authorizedProperties[0]?.id || '');
      } catch {
        if (!cancelled) {
          setError('Failed to load your properties.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    loadProperties();
    return () => { cancelled = true; };
  }, [identity]);

  return (
    <div className="p-4 lg:p-6 min-h-full">
      <div className="max-w-7xl mx-auto">
        <h1 className="text-2xl font-bold mb-6 text-gray-900">Property Management Dashboard</h1>

        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 lg:p-6">
          <div className="mb-6">
            <div className="flex flex-col sm:flex-row sm:justify-between sm:items-start gap-4">
              <div>
                <h2 className="text-lg lg:text-xl font-medium text-gray-900 mb-2">Revenue Overview</h2>
                <p className="text-sm lg:text-base text-gray-600">
                  Monthly performance insights for your properties
                </p>
              </div>
              
              {/* Property Selector */}
              <div className="flex flex-col sm:flex-row gap-3">
                <div className="flex flex-col">
                  <label htmlFor="dashboard-property" className="text-xs font-medium text-gray-700 mb-1">Select Property</label>
                  <select
                    id="dashboard-property"
                    value={selectedProperty}
                    onChange={(e) => setSelectedProperty(e.target.value)}
                    disabled={loading || properties.length === 0}
                    className="block w-full sm:w-auto min-w-[200px] px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 text-sm"
                  >
                    {properties.length === 0 && <option value="">{loading ? 'Loading properties...' : 'No properties'}</option>}
                    {properties.map((property) => (
                      <option key={property.id} value={property.id}>
                        {property.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex flex-col">
                  <label htmlFor="dashboard-month" className="text-xs font-medium text-gray-700 mb-1">Reporting Month</label>
                  <input
                    id="dashboard-month"
                    type="month"
                    value={period}
                    onChange={(e) => setPeriod(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 text-sm"
                  />
                </div>
              </div>
            </div>
          </div>

          <div className="space-y-6">
            {error && <p role="alert" className="p-4 text-red-500 bg-red-50 rounded-lg">{error}</p>}
            {!loading && !error && properties.length === 0 && <p className="text-sm text-gray-600">No properties are available for your account.</p>}
            {!loading && !error && selectedProperty && properties.some(property => property.id === selectedProperty) && period && (
              <RevenueSummary key={`${identity}:${selectedProperty}:${period}`} propertyId={selectedProperty} year={year} month={month} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default Dashboard;
